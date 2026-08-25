"""Persistent terminal sessions — Requirement 5.

Real shells. The claim under test is that state survives between calls, which
is exactly what a fake would fabricate for free.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from plugkit import Context

from pydsh.capability import TerminalClosedError, TerminalService

pytestmark = pytest.mark.asyncio


async def mounted(**config) -> tuple[Context, object]:
    ctx = Context()
    fiber = await ctx.plugin(TerminalService, config)
    return ctx, fiber


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


# --------------------------------------------------------------------------- #
# The feature: state between calls (R5.1, R5.2)
# --------------------------------------------------------------------------- #
async def test_a_session_keeps_its_working_directory_between_commands(tmp_path):
    """The whole reason a terminal is not just repeated `shell.execute`."""
    (tmp_path / "sub").mkdir()
    root, _ = await mounted()
    session = await root.terminal.spawn(cwd=str(tmp_path))

    await session.send("cd sub")
    output = await session.send("pwd")

    assert "sub" in output
    await session.close()


async def test_a_session_keeps_its_variables(tmp_path):
    root, _ = await mounted()
    session = await root.terminal.spawn(cwd=str(tmp_path))

    await session.send("MARKER=remembered")
    assert "remembered" in await session.send("echo $MARKER")
    await session.close()


async def test_send_returns_the_output_of_the_command(tmp_path):
    root, _ = await mounted()
    session = await root.terminal.spawn(cwd=str(tmp_path))
    assert "hello" in await session.send("echo hello")
    await session.close()


async def test_a_session_has_an_id_and_appears_in_the_listing(tmp_path):
    root, _ = await mounted()
    session = await root.terminal.spawn(id="work", cwd=str(tmp_path))
    assert root.terminal.get("work") is session
    assert root.terminal.list() == [
        {"id": "work", "cwd": str(tmp_path), "closed": False}
    ]
    await session.close()


async def test_spawning_a_duplicate_id_while_open_is_refused(tmp_path):
    root, _ = await mounted()
    session = await root.terminal.spawn(id="work", cwd=str(tmp_path))
    with pytest.raises(ValueError, match="already open"):
        await root.terminal.spawn(id="work")
    await session.close()


async def test_an_unknown_session_names_the_open_ones(tmp_path):
    root, _ = await mounted()
    session = await root.terminal.spawn(id="work", cwd=str(tmp_path))
    with pytest.raises(KeyError) as caught:
        root.terminal.get("other")
    assert "work" in str(caught.value)
    await session.close()


# --------------------------------------------------------------------------- #
# Reading without blocking (R5.3)
# --------------------------------------------------------------------------- #
async def test_a_command_with_no_output_returns_at_once(tmp_path):
    """The defect a settle-based read has: `cd` prints nothing, so silence
    cannot distinguish it from a command that has not started."""
    import time

    root, _ = await mounted()
    session = await root.terminal.spawn(cwd=str(tmp_path))

    started = time.monotonic()
    assert await session.send("cd .") == ""
    assert time.monotonic() - started < 1.0

    await session.close()


async def test_read_available_returns_promptly_when_there_is_nothing(tmp_path):
    root, _ = await mounted()
    session = await root.terminal.spawn(cwd=str(tmp_path))
    await session.send("echo drain")

    assert await session.read_available() == ""
    await session.close()


# --------------------------------------------------------------------------- #
# Closing (R5.4, R5.5)
# --------------------------------------------------------------------------- #
async def test_a_closed_session_refuses_further_calls(tmp_path):
    root, _ = await mounted()
    session = await root.terminal.spawn(cwd=str(tmp_path))
    await session.close()

    assert session.closed is True
    with pytest.raises(TerminalClosedError):
        await session.send("echo nope")
    with pytest.raises(TerminalClosedError):
        await session.read_available()


async def test_closing_really_ends_the_process(tmp_path):
    root, _ = await mounted()
    session = await root.terminal.spawn(cwd=str(tmp_path))
    pid = session._process.pid

    await session.close()
    await asyncio.sleep(0.3)
    assert not alive(pid)


async def test_closing_twice_is_fine(tmp_path):
    root, _ = await mounted()
    session = await root.terminal.spawn(cwd=str(tmp_path))
    await session.close()
    await session.close()


async def test_closing_through_the_service_forgets_the_session(tmp_path):
    root, _ = await mounted()
    await root.terminal.spawn(id="work", cwd=str(tmp_path))
    await root.terminal.close("work")
    assert root.terminal.list() == []


async def test_close_all_ends_everything(tmp_path):
    root, _ = await mounted()
    first = await root.terminal.spawn(id="a", cwd=str(tmp_path))
    second = await root.terminal.spawn(id="b", cwd=str(tmp_path))
    pids = [first._process.pid, second._process.pid]

    await root.terminal.close_all()
    await asyncio.sleep(0.3)
    assert root.terminal.list() == []
    assert not any(alive(pid) for pid in pids)


async def test_a_session_whose_shell_exits_reports_itself_closed(tmp_path):
    """R5.4 — a dead shell must be visible, not hang the next send."""
    root, _ = await mounted()
    session = await root.terminal.spawn(cwd=str(tmp_path))
    await session.send("exit")
    await asyncio.sleep(0.2)
    assert session.closed is True


# --------------------------------------------------------------------------- #
# R5.6 — unmounting
# --------------------------------------------------------------------------- #
async def test_unmounting_the_service_ends_every_session(tmp_path):
    """A live shell that outlives the harness is a leak with a shell attached."""
    root, fiber = await mounted()
    first = await root.terminal.spawn(id="a", cwd=str(tmp_path))
    second = await root.terminal.spawn(id="b", cwd=str(tmp_path))
    pids = [first._process.pid, second._process.pid]

    fiber.dispose()
    for _ in range(4):
        await asyncio.sleep(0)
    await asyncio.sleep(0.3)

    assert not any(alive(pid) for pid in pids)
