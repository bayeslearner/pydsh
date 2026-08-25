"""Settings, credentials, commands, identity — Requirements 3 to 6."""

from __future__ import annotations

import os

import pytest

from plugkit import Context, PointsService, ToolsService

from pydsh.agent import AGENT_LOOP_SETTINGS, Agent, AgentOptions
from pydsh.operating import (
    CREDENTIALS_UPDATED,
    AnonymousUserId,
    CommandResult,
    Commands,
    CredentialRefError,
    Credentials,
    Settings,
    UnknownNamespaceError,
    get_or_create_anonymous_user_id,
)

pytestmark = pytest.mark.asyncio


async def mounted(*plugins) -> Context:
    root = Context()
    for plugin in plugins:
        await root.plugin(plugin)
    return root


# --------------------------------------------------------------------------- #
# Settings (R3)
# --------------------------------------------------------------------------- #
def positive_limit(value):
    limit = value.get("max_parallel_tool_calls")
    if not isinstance(limit, int) or limit < 1:
        raise ValueError("max_parallel_tool_calls must be a positive integer")
    return value


async def test_a_section_starts_from_its_base():
    root = await mounted(Settings)
    root.settings.register("agent-loop", base={"max_parallel_tool_calls": 4})
    assert root.settings.get("agent-loop") == {"max_parallel_tool_calls": 4}


async def test_a_write_is_validated_and_visible():
    root = await mounted(Settings)
    root.settings.register("agent-loop", positive_limit, {"max_parallel_tool_calls": 1})
    root.settings.set("agent-loop", {"max_parallel_tool_calls": 8})
    assert root.settings.get("agent-loop")["max_parallel_tool_calls"] == 8


async def test_a_rejected_write_leaves_the_old_value(  ):
    """Property 3 (I4) — validate first, so nothing half-applies."""
    root = await mounted(Settings)
    root.settings.register("agent-loop", positive_limit, {"max_parallel_tool_calls": 2})
    fired = []
    root.settings.scope("agent-loop").watch(lambda: fired.append(1))

    with pytest.raises(ValueError):
        root.settings.set("agent-loop", {"max_parallel_tool_calls": 0})

    assert root.settings.get("agent-loop")["max_parallel_tool_calls"] == 2
    assert fired == []


async def test_a_base_value_is_validated_too():
    root = await mounted(Settings)
    with pytest.raises(ValueError):
        root.settings.register("bad", positive_limit, {"max_parallel_tool_calls": 0})


async def test_watchers_fire_on_an_accepted_write():
    root = await mounted(Settings)
    scope = root.settings.register("x", base=1)
    seen = []
    scope.watch(lambda: seen.append(scope.get()))
    scope.set(2)
    scope.set(3)
    assert seen == [2, 3]


async def test_a_failing_watcher_does_not_stop_the_others():
    root = await mounted(Settings)
    scope = root.settings.register("x", base=1)
    seen = []

    scope.watch(lambda: (_ for _ in ()).throw(RuntimeError("watcher bug")))
    scope.watch(lambda: seen.append(scope.get()))
    scope.set(2)
    assert seen == [2]


async def test_unwatching_removes_only_its_own_registration():
    root = await mounted(Settings)
    scope = root.settings.register("x", base=1)
    seen = []

    def watcher():
        seen.append(1)

    off = scope.watch(watcher)
    scope.watch(watcher)
    assert off() is True
    assert off() is False
    scope.set(2)
    assert len(seen) == 1


async def test_an_unregistered_namespace_names_the_registered_ones():
    root = await mounted(Settings)
    root.settings.register("agent-loop", base={})
    with pytest.raises(UnknownNamespaceError) as caught:
        root.settings.get("nope")
    assert "agent-loop" in str(caught.value)


# --------------------------------------------------------------------------- #
# R3.7 — the loop reads its limit live
# --------------------------------------------------------------------------- #
async def test_the_loop_takes_its_parallel_limit_from_settings():
    """Spec 03 had to leave this as a constructor argument. Now it is live."""
    root = Context()
    await root.plugin(Settings)
    from pydsh.llm import LlmService
    from pydsh.session import SessionStore

    await root.plugin(SessionStore)
    await root.plugin(LlmService)
    await root.plugin(PointsService)
    await root.plugin(ToolsService)

    session = root.sessions.create()
    agent = Agent(root, session, AgentOptions(provider="p", model="m"))
    assert agent._parallel_limit() == 1  # from options

    root.settings.register(AGENT_LOOP_SETTINGS, base={"max_parallel_tool_calls": 6})
    assert agent._parallel_limit() == 6  # the setting wins, on an existing agent

    root.settings.set(AGENT_LOOP_SETTINGS, {"max_parallel_tool_calls": 2})
    assert agent._parallel_limit() == 2  # and a change lands without a new agent


async def test_the_loop_falls_back_to_options_without_settings():
    from pydsh.llm import LlmService
    from pydsh.session import SessionStore

    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(LlmService)
    session = root.sessions.create()
    agent = Agent(root, session, AgentOptions(provider="p", model="m", max_parallel_tool_calls=3))
    assert agent._parallel_limit() == 3


# --------------------------------------------------------------------------- #
# Credentials (R4)
# --------------------------------------------------------------------------- #
async def test_an_unset_ref_resolves_to_nothing():
    root = await mounted(Credentials)
    assert await root.credentials.resolve("NOWHERE_AT_ALL") is None


async def test_a_stored_value_resolves_with_its_source():
    root = await mounted(Credentials)
    await root.credentials.set("API_KEY", "sk-secret")
    assert await root.credentials.resolve("API_KEY") == {
        "value": "sk-secret",
        "source": "store",
    }


async def test_the_environment_is_the_fallback(monkeypatch):
    root = await mounted(Credentials)
    monkeypatch.setenv("API_KEY", "from-env")
    assert await root.credentials.resolve("API_KEY") == {
        "value": "from-env",
        "source": "env",
    }


async def test_a_stored_value_beats_the_environment(monkeypatch):
    """R4.2 — the store is a deliberate override, the environment the floor."""
    root = await mounted(Credentials)
    monkeypatch.setenv("API_KEY", "from-env")
    await root.credentials.set("API_KEY", "explicit")
    assert (await root.credentials.resolve("API_KEY"))["value"] == "explicit"


async def test_resolution_happens_per_call(monkeypatch):
    """R4.3 — a rotated key must not need a restart."""
    root = await mounted(Credentials)
    monkeypatch.setenv("API_KEY", "first")
    assert (await root.credentials.resolve("API_KEY"))["value"] == "first"
    monkeypatch.setenv("API_KEY", "second")
    assert (await root.credentials.resolve("API_KEY"))["value"] == "second"


async def test_an_illegal_ref_is_rejected():
    root = await mounted(Credentials)
    for bad in ("has space", "1leading", "dash-ed", ""):
        with pytest.raises(CredentialRefError):
            await root.credentials.resolve(bad)


async def test_changes_are_broadcast():
    root = await mounted(Credentials)
    seen = []
    root.on(CREDENTIALS_UPDATED, lambda ref: seen.append(ref))
    await root.credentials.set("API_KEY", "x")
    await root.credentials.delete("API_KEY")
    assert seen == ["API_KEY", "API_KEY"]


async def test_delete_reports_whether_anything_went(monkeypatch):
    root = await mounted(Credentials)
    assert await root.credentials.delete("API_KEY") is False
    await root.credentials.set("API_KEY", "x")
    assert await root.credentials.delete("API_KEY") is True


async def test_delete_never_touches_the_environment(monkeypatch):
    """R4.6 — the process's environment is not this service's to edit."""
    root = await mounted(Credentials)
    monkeypatch.setenv("API_KEY", "from-env")
    await root.credentials.set("API_KEY", "stored")
    await root.credentials.delete("API_KEY")
    assert (await root.credentials.resolve("API_KEY"))["source"] == "env"
    assert os.environ["API_KEY"] == "from-env"


async def test_describe_never_carries_the_value():
    """R4.7 — describe exists to be shown; showing it must not leak the key."""
    root = await mounted(Credentials)
    await root.credentials.set("API_KEY", "sk-super-secret")
    described = await root.credentials.describe("API_KEY")

    assert described == {"ref": "API_KEY", "available": True, "source": "store"}
    assert "sk-super-secret" not in repr(described)


async def test_describe_reports_an_absent_ref():
    root = await mounted(Credentials)
    assert await root.credentials.describe("NOWHERE_AT_ALL") == {
        "ref": "NOWHERE_AT_ALL",
        "available": False,
    }


# --------------------------------------------------------------------------- #
# Commands (R5)
# --------------------------------------------------------------------------- #
async def test_a_registered_command_runs():
    root = await mounted(Commands)
    root.commands.register("plan", "Enter or leave plan mode",
                           lambda inv: CommandResult.success(f"plan {inv.raw_input}"))
    result = await root.commands.invoke("plan", raw_input="off")
    assert result == CommandResult("success", "plan off", None)


async def test_an_async_handler_works_too():
    root = await mounted(Commands)

    async def handler(invocation):
        return CommandResult.success("done")

    root.commands.register("compact", "Compact the session", handler)
    assert (await root.commands.invoke("compact")).text == "done"


async def test_an_empty_name_is_rejected():
    root = await mounted(Commands)
    with pytest.raises(ValueError, match="needs a name"):
        root.commands.register("", "nothing", lambda inv: CommandResult.success(""))


async def test_an_unknown_command_lists_what_there_is():
    root = await mounted(Commands)
    root.commands.register("plan", "…", lambda inv: CommandResult.success(""))
    result = await root.commands.invoke("nope")
    assert result.kind == "error"
    assert "/plan" in result.text


async def test_a_raising_handler_comes_back_as_text():
    """R5.5 (I5) — a person typed this; an exception is not an answer."""
    root = await mounted(Commands)
    root.commands.register("boom", "…",
                           lambda inv: (_ for _ in ()).throw(RuntimeError("no good")))
    result = await root.commands.invoke("boom")
    assert result.kind == "error"
    assert "no good" in result.text


async def test_a_handler_returning_the_wrong_thing_is_an_error_result():
    root = await mounted(Commands)
    root.commands.register("odd", "…", lambda inv: "just a string")
    result = await root.commands.invoke("odd")
    assert result.kind == "error"
    assert "not a CommandResult" in result.text


async def test_the_invocation_carries_the_agent_and_signal():
    root = await mounted(Commands)
    seen = {}

    def handler(invocation):
        seen["agent"] = invocation.agent
        seen["signal"] = invocation.signal
        return CommandResult.success("ok")

    root.commands.register("probe", "…", handler)
    await root.commands.invoke("probe", agent="an-agent", signal="a-signal")
    assert seen == {"agent": "an-agent", "signal": "a-signal"}


async def test_listing_and_disposing():
    root = await mounted(Commands)
    dispose = root.commands.register("plan", "Plan mode", lambda inv: None)
    root.commands.register("compact", "Compact", lambda inv: None)
    assert root.commands.list() == [
        {"name": "compact", "description": "Compact"},
        {"name": "plan", "description": "Plan mode"},
    ]
    assert dispose() is True
    assert root.commands.has("plan") is False


async def test_re_registering_replaces():
    """A plugin reloading takes its own command back over."""
    root = await mounted(Commands)
    root.commands.register("plan", "old", lambda inv: CommandResult.success("old"))
    root.commands.register("plan", "new", lambda inv: CommandResult.success("new"))
    assert (await root.commands.invoke("plan")).text == "new"


# --------------------------------------------------------------------------- #
# Identity (R6)
# --------------------------------------------------------------------------- #
async def test_an_id_is_created_and_then_stable(tmp_path):
    first = get_or_create_anonymous_user_id(tmp_path)
    second = get_or_create_anonymous_user_id(tmp_path)
    assert first == second
    assert (tmp_path / ".anonymous-user-id").read_text().strip() == first


async def test_a_corrupt_stored_id_is_replaced(tmp_path):
    """R6.2 — a hand-edited file must not propagate into every record."""
    (tmp_path / ".anonymous-user-id").write_text("not-a-uuid")
    fresh = get_or_create_anonymous_user_id(tmp_path)
    assert fresh != "not-a-uuid"
    assert get_or_create_anonymous_user_id(tmp_path) == fresh


async def test_an_unwritable_home_still_yields_an_id(tmp_path):
    """R6.3 — telemetry loses continuity; the harness still starts."""
    unwritable = tmp_path / "readonly"
    unwritable.mkdir()
    unwritable.chmod(0o500)
    try:
        value = get_or_create_anonymous_user_id(unwritable)
        assert len(value) == 36
    finally:
        unwritable.chmod(0o700)


async def test_the_service_exposes_the_value(tmp_path):
    root = Context()
    await root.plugin(AnonymousUserId, {"home": str(tmp_path)})
    assert root.anonymous_user_id.value == get_or_create_anonymous_user_id(tmp_path)
