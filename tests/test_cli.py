"""The CLI — Requirement 3, property 3.

The rule under test throughout: an expected failure prints one line and exits
non-zero. Someone who typed a wrong path should read a sentence, not a stack.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from pydsh.cli import COMMANDS, EXIT_FAILURE, build_parser, main

# Not asyncio tests: `main` is a console entry point and owns its own loop, so
# calling it from inside one would be testing something no user can do.


PROFILE_MODULE = '''
from plugkit import PointsService, ToolsService
from pydsh import (AgentLoop, AgentRegistry, LlmService, SessionStore, TokenMeter,
                   ChunkType, StreamChunk, LlmAdapter, LlmProviderInfo)


class Fixed(LlmAdapter):
    async def stream(self, options):
        yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text="a considered reply")
        yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})

    def provider_info(self, provider):
        return LlmProviderInfo(id=provider, name=provider)


def register(ctx, config=None):
    ctx.llm.register_adapter(["acme"], Fixed())


register.inject = ["llm"]
register.name = "fixed-adapter"

PROFILE = [
    (PointsService, {}),
    (ToolsService, {}),
    (SessionStore, {}),
    (LlmService, {}),
    (TokenMeter, {}),
    (AgentRegistry, {}),
    (AgentLoop, {}),
    (register, {}),
]
'''


@pytest.fixture
def profile(tmp_path):
    path = tmp_path / "cli_profile.py"
    path.write_text(PROFILE_MODULE)
    return str(path)


def run(argv, tmp_path) -> int:
    return main([*argv, "--home", str(tmp_path / "home")])


# --------------------------------------------------------------------------- #
# R3.1 — the shape
# --------------------------------------------------------------------------- #
def test_every_command_is_wired():
    """R3.1."""
    assert set(COMMANDS) == {"chat", "sessions", "runtime", "gateway"}


def test_no_command_prints_help_and_fails(capsys):
    """A person who typed `pydsh` needs to know what it does."""
    assert main([]) == EXIT_FAILURE
    assert "usage: pydsh" in capsys.readouterr().err


def test_every_command_accepts_the_shared_options():
    """R3.5."""
    parser = build_parser()
    for command in COMMANDS:
        arguments = parser.parse_args(
            ["--profile", "p", "--home", "h", "--log-level", "debug", command]
        )
        assert arguments.profile == "p" and arguments.home == "h"
        assert arguments.log_level == "debug"


# --------------------------------------------------------------------------- #
# R3.2 — chat
# --------------------------------------------------------------------------- #
def test_chat_runs_a_prompt_and_prints_the_answer(profile, tmp_path, capsys):
    """R3.2 — end to end, over the profile's own adapter."""
    code = run(
        ["--profile", profile, "chat", "--provider", "acme", "--model", "a-1", "hello", "there"],
        tmp_path,
    )
    assert code == 0
    assert capsys.readouterr().out.strip() == "a considered reply"


def test_chat_reads_a_prompt_from_stdin(profile, tmp_path, capsys, monkeypatch):
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO("what is it?\n"))
    code = run(["--profile", profile, "chat", "--provider", "acme", "--model", "a-1"], tmp_path)
    assert code == 0
    assert "a considered reply" in capsys.readouterr().out


def test_chat_with_nothing_to_say_fails_readably(profile, tmp_path, capsys, monkeypatch):
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert run(["--profile", profile, "chat"], tmp_path) == EXIT_FAILURE
    captured = capsys.readouterr()
    assert "nothing to say" in captured.err
    assert "Traceback" not in captured.err


def test_chat_json_output(profile, tmp_path, capsys):
    """R3.7."""
    code = run(
        ["--json", "--profile", profile, "chat", "--provider", "acme", "--model", "a-1", "hi"],
        tmp_path,
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["final_response"] == "a considered reply"
    assert payload["event_count"] > 0


def test_chat_names_its_session(profile, tmp_path, capsys):
    code = run(
        ["--json", "--profile", profile, "chat", "--session", "my-chat",
         "--provider", "acme", "--model", "a-1", "hi"],
        tmp_path,
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["session_id"] == "my-chat"


# --------------------------------------------------------------------------- #
# R3.3 — sessions
# --------------------------------------------------------------------------- #
def test_sessions_lists_nothing_when_there_is_nothing(profile, tmp_path, capsys):
    """R3.3."""
    assert run(["--profile", profile, "sessions"], tmp_path) == 0
    assert "no sessions" in capsys.readouterr().out


def test_sessions_json_output(profile, tmp_path, capsys):
    assert run(["--json", "--profile", profile, "sessions"], tmp_path) == 0
    assert json.loads(capsys.readouterr().out) == {"sessions": []}


def test_showing_an_absent_session_fails_readably(profile, tmp_path, capsys):
    assert run(["--profile", profile, "sessions", "--session", "nope"], tmp_path) == EXIT_FAILURE
    captured = capsys.readouterr()
    assert "no session 'nope'" in captured.err
    assert "Traceback" not in captured.err


def test_a_profile_with_no_session_store_says_so(tmp_path, capsys):
    module = tmp_path / "bare.py"
    module.write_text("from plugkit import PointsService\nPROFILE = [(PointsService, {})]\n")
    assert run(["--profile", str(module), "sessions"], tmp_path) == EXIT_FAILURE
    assert "no session store" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# R3.6 — expected failures (property 3)
# --------------------------------------------------------------------------- #
def test_a_missing_profile_is_one_line(tmp_path, capsys):
    """Property 3 (R3.6, I5)."""
    assert run(["--profile", str(tmp_path / "nowhere.py"), "sessions"], tmp_path) == EXIT_FAILURE
    captured = capsys.readouterr()
    assert captured.err.startswith("pydsh: ")
    assert len(captured.err.strip().splitlines()) == 1
    assert "Traceback" not in captured.err


def test_a_bad_profile_entry_is_one_line(tmp_path, capsys):
    module = tmp_path / "bad.py"
    module.write_text("PROFILE = [(None, {})]\n")
    assert run(["--profile", str(module), "sessions"], tmp_path) == EXIT_FAILURE
    captured = capsys.readouterr()
    assert "entry 0" in captured.err
    assert "Traceback" not in captured.err


def test_an_unroutable_provider_is_one_line(profile, tmp_path, capsys):
    assert run(
        ["--profile", profile, "chat", "--provider", "nowhere", "--model", "m", "hi"],
        tmp_path,
    ) == EXIT_FAILURE
    captured = capsys.readouterr()
    assert "nowhere" in captured.err
    assert "Traceback" not in captured.err


def test_an_unexpected_failure_still_raises(profile, tmp_path, monkeypatch):
    """A bug is exactly the case where a stack is what someone needs."""
    from pydsh import cli

    async def explode(arguments):
        raise ZeroDivisionError("a real bug")

    monkeypatch.setitem(cli.COMMANDS, "sessions", explode)
    with pytest.raises(ZeroDivisionError):
        run(["--profile", profile, "sessions"], tmp_path)


# --------------------------------------------------------------------------- #
# R3.4 — the servers are reachable from here
# --------------------------------------------------------------------------- #
def test_the_gateway_command_parses_its_options():
    """R3.4."""
    arguments = build_parser().parse_args(
        ["gateway", "--host", "0.0.0.0", "--port", "9000", "--max-connections", "8"]
    )
    assert arguments.host == "0.0.0.0"
    assert arguments.port == 9000 and arguments.max_connections == 8


def test_the_gateway_command_reports_a_missing_extra(profile, tmp_path, capsys, monkeypatch):
    """R3.6 — one line naming what to install."""
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "websockets":
            raise ImportError("no websockets here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    assert run(["--profile", profile, "gateway"], tmp_path) == EXIT_FAILURE
    captured = capsys.readouterr()
    assert "pydsh[ws]" in captured.err
    assert "Traceback" not in captured.err
