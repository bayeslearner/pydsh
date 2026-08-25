"""Home, the layered environment, and profiles — Requirements 1–3.

The tests that matter are the refusals and the precedences: a blank override
that must not become the working directory, a file that must not decide how the
process boots, and a profile that must not half-mount.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from plugkit import Context, PointsService, Service

from pydsh.boot import (
    BOOTSTRAP_PREFIXES,
    ENV_FILE_NAME,
    HOME_DIR_NAME,
    HOME_DISPLAY,
    HOME_ENV,
    EnvFileError,
    HomePathError,
    ProfileEntry,
    ProfileError,
    core_profile,
    expand_home_path,
    home_display,
    home_path,
    interpolate_env,
    load_layered_env,
    load_profile,
    mount_profile,
    parse_env,
    resolve_home,
    resolve_profile,
    unmount,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# R1 — home
# --------------------------------------------------------------------------- #
async def test_home_defaults_under_the_user_home(monkeypatch):
    """R1.1."""
    monkeypatch.delenv(HOME_ENV, raising=False)
    assert resolve_home() == os.path.join(os.path.expanduser("~"), HOME_DIR_NAME)


async def test_the_environment_overrides_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    assert resolve_home() == str(tmp_path)


async def test_an_explicit_argument_beats_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv(HOME_ENV, "/from/env")
    assert resolve_home(str(tmp_path)) == str(tmp_path)


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
async def test_a_blank_override_is_not_an_override(monkeypatch, blank):
    """R1.2, I2 — otherwise home becomes wherever the process started."""
    monkeypatch.setenv(HOME_ENV, blank)
    resolved = resolve_home()
    assert resolved == os.path.join(os.path.expanduser("~"), HOME_DIR_NAME)
    assert resolved != os.getcwd()


async def test_a_blank_argument_falls_through_to_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    assert resolve_home("  ") == str(tmp_path)


async def test_a_tilde_expands():
    """R1.3."""
    assert expand_home_path("~") == os.path.expanduser("~")
    assert expand_home_path("~/x") == os.path.join(os.path.expanduser("~"), "x")
    assert expand_home_path("/absolute") == "/absolute"
    assert expand_home_path("~notauser/x") == "~notauser/x"


async def test_home_path_joins_beneath_home(tmp_path, monkeypatch):
    """R1.4."""
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    assert home_path("sessions", "log.db") == str(tmp_path / "sessions" / "log.db")


async def test_a_segment_that_escapes_home_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    for escape in ("..", "../..", "../elsewhere"):
        with pytest.raises(HomePathError):
            home_path(escape)


async def test_a_symlink_out_of_home_is_refused(tmp_path, monkeypatch):
    """Resolved on both sides, so a link is not the way out either."""
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "link").symlink_to(outside)
    monkeypatch.setenv(HOME_ENV, str(home))

    with pytest.raises(HomePathError):
        home_path("link")


async def test_home_display_never_reveals_a_machine_path(tmp_path, monkeypatch):
    """R1.5 — a status line is read by someone who should not learn the layout."""
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    assert home_display(str(tmp_path)) == HOME_DISPLAY
    assert home_display(str(tmp_path / "sessions")) == f"{HOME_DISPLAY}/sessions"
    assert str(tmp_path) not in home_display(str(tmp_path / "a" / "b"))


async def test_home_display_falls_back_to_the_user_home(monkeypatch, tmp_path):
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    shown = home_display(os.path.join(os.path.expanduser("~"), "elsewhere"))
    assert shown == "~/elsewhere"


# --------------------------------------------------------------------------- #
# R2 — the layered environment
# --------------------------------------------------------------------------- #
async def test_parse_env_reads_key_value_text():
    """R2.1."""
    parsed = parse_env(
        "\n".join(
            [
                "# a comment",
                "",
                "PLAIN=value",
                "QUOTED='single'",
                'DOUBLE="double"',
                "SPACED  =  padded  ",
                "not a pair",
            ]
        )
    )
    assert parsed == {
        "PLAIN": "value",
        "QUOTED": "single",
        "DOUBLE": "double",
        "SPACED": "padded",
    }


async def test_the_layers_merge(tmp_path):
    """R2.2."""
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    home.mkdir()
    cwd.mkdir()
    (cwd / ENV_FILE_NAME).write_text("FROM_CWD=yes\n")
    (home / ENV_FILE_NAME).write_text("FROM_HOME=yes\n")

    merged = load_layered_env(str(cwd), str(home), {"FROM_SHELL": "yes"})
    assert merged["FROM_SHELL"] == "yes"
    assert merged["FROM_CWD"] == "yes"
    assert merged["FROM_HOME"] == "yes"


async def test_an_inherited_value_wins(tmp_path):
    """Property 1 (R2.3, I4) — an override a file could beat is no override."""
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    home.mkdir()
    cwd.mkdir()
    (cwd / ENV_FILE_NAME).write_text("SHARED=from-cwd\n")
    (home / ENV_FILE_NAME).write_text("SHARED=from-home\n")

    merged = load_layered_env(str(cwd), str(home), {"SHARED": "from-shell"})
    assert merged["SHARED"] == "from-shell"


async def test_a_nearer_file_wins_over_a_further_one(tmp_path):
    """R2.4."""
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    home.mkdir()
    cwd.mkdir()
    (cwd / ENV_FILE_NAME).write_text("SHARED=from-cwd\n")
    (home / ENV_FILE_NAME).write_text("SHARED=from-home\n")

    merged = load_layered_env(str(cwd), str(home), {})
    assert merged["SHARED"] == "from-cwd"


@pytest.mark.parametrize("prefix", BOOTSTRAP_PREFIXES)
async def test_a_file_cannot_set_a_bootstrap_variable(tmp_path, prefix):
    """R2.5, I3 — the variable that decided where this file was looked for."""
    cwd = tmp_path / "work"
    home = tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    (cwd / ENV_FILE_NAME).write_text(f"{prefix}SOMETHING=/elsewhere\n")

    with pytest.raises(EnvFileError) as caught:
        load_layered_env(str(cwd), str(home), {})
    assert f"{prefix}SOMETHING" in str(caught.value)
    assert ENV_FILE_NAME in str(caught.value)


async def test_a_missing_env_file_is_not_an_error(tmp_path):
    merged = load_layered_env(str(tmp_path), str(tmp_path), {"A": "1"})
    assert merged == {"A": "1"}


async def test_interpolation_substitutes_both_spellings():
    """R2.6."""
    env = {"NAME": "world"}
    assert interpolate_env("hello ${NAME}", env) == "hello world"
    assert interpolate_env("hello $NAME", env) == "hello world"


async def test_interpolation_reaches_into_structures():
    env = {"KEY": "sk-1"}
    resolved = interpolate_env(
        {"a": ["${KEY}", {"b": "$KEY"}], "c": ("${KEY}",), "d": 7}, env
    )
    assert resolved == {"a": ["sk-1", {"b": "sk-1"}], "c": ("sk-1",), "d": 7}


async def test_an_unset_variable_is_left_as_written():
    """A missing secret must not become a configured-looking blank."""
    assert interpolate_env("${NOT_SET}", {}) == "${NOT_SET}"
    assert interpolate_env("prefix-$NOT_SET", {}) == "prefix-$NOT_SET"


# --------------------------------------------------------------------------- #
# R3 — profiles
# --------------------------------------------------------------------------- #
class Alpha(Service):
    provide = "alpha"

    def __init__(self, ctx, config=None):
        super().__init__(ctx)
        self.config = config or {}


class Beta(Service):
    provide = "beta"

    def __init__(self, ctx, config=None):
        super().__init__(ctx)
        self.config = config or {}


class Exploding(Service):
    provide = "exploding"

    def __init__(self, ctx, config=None):
        super().__init__(ctx)
        raise RuntimeError("this one cannot mount")


async def test_a_profile_accepts_several_spellings():
    """R3.1."""
    entries = resolve_profile(
        [Alpha, (Beta,), (Alpha, {"x": 1}), ProfileEntry(Beta, {"y": 2})]
    )
    assert [e.plugin for e in entries] == [Alpha, Beta, Alpha, Beta]
    assert [e.config for e in entries] == [{}, {}, {"x": 1}, {"y": 2}]


async def test_entries_are_interpolated_before_mounting():
    """R3.3."""
    entries = resolve_profile([(Alpha, {"key": "${SECRET}"})], {"SECRET": "sk-1"})
    assert entries[0].config == {"key": "sk-1"}


@pytest.mark.parametrize(
    "profile,expected",
    [
        ([(Alpha, "not a mapping")], "must be a mapping"),
        ([(Alpha, {}, "extra")], "must be (plugin, config)"),
        ([()], "must be (plugin, config)"),
        ([None], "has no plugin"),
    ],
)
async def test_a_bad_entry_names_its_index(profile, expected):
    with pytest.raises(ProfileError) as caught:
        resolve_profile(profile)
    assert expected in str(caught.value)
    assert "entry 0" in str(caught.value)


async def test_a_profile_that_is_not_a_list_is_refused():
    with pytest.raises(ProfileError) as caught:
        resolve_profile({"alpha": Alpha})
    assert "list of entries" in str(caught.value)


async def test_no_profile_resolves_to_nothing():
    assert resolve_profile(None) == []


async def test_a_profile_loads_from_a_file(tmp_path):
    """R3.2."""
    module = tmp_path / "my_profile.py"
    module.write_text(
        "from plugkit import PointsService\nPROFILE = [(PointsService, {})]\n"
    )
    entries = resolve_profile(str(module))
    assert entries[0].plugin is PointsService


async def test_a_file_with_no_profile_says_so(tmp_path):
    module = tmp_path / "empty.py"
    module.write_text("X = 1\n")
    with pytest.raises(ProfileError) as caught:
        load_profile(str(module))
    assert "exposes no PROFILE" in str(caught.value)


async def test_a_missing_profile_file_says_so(tmp_path):
    with pytest.raises(ProfileError) as caught:
        load_profile(str(tmp_path / "nowhere.py"))
    assert "no profile at" in str(caught.value)


async def test_an_unimportable_module_says_so():
    with pytest.raises(ProfileError) as caught:
        load_profile("pydsh.no.such.module")
    assert "could not be loaded" in str(caught.value)


async def test_mounting_happens_in_order():
    """R3.4."""
    ctx = Context()
    mounted = await mount_profile(ctx, resolve_profile([Alpha, (Beta, {"y": 2})]))
    assert ctx.alpha is not None and ctx.beta.config == {"y": 2}
    assert len(mounted) == 2

    await unmount(mounted)
    assert ctx.beta is None and ctx.alpha is None


async def test_a_failed_mount_leaves_nothing_mounted():
    """Property 2 (R3.3, I5) — a half-built context must not escape."""
    ctx = Context()
    with pytest.raises(ProfileError) as caught:
        await mount_profile(ctx, resolve_profile([Alpha, Exploding, Beta]))
    assert "entry 1" in str(caught.value) and "Exploding" in str(caught.value)
    assert ctx.alpha is None, "the entry before the failure stayed mounted"
    assert ctx.beta is None


async def test_the_core_profile_is_a_list_a_consumer_can_extend():
    """R3.5."""
    profile = core_profile()
    assert isinstance(profile, list) and profile
    extended = [*profile, (Alpha, {})]
    entries = resolve_profile(extended)
    assert entries[-1].plugin is Alpha


async def test_the_core_profile_mounts():
    ctx = Context()
    mounted = await mount_profile(ctx, resolve_profile(core_profile()))
    try:
        for name in ("sessions", "llm", "agents", "agent_loop", "tools", "storage"):
            assert getattr(ctx, name) is not None, f"{name} did not mount"
    finally:
        await unmount(mounted)
