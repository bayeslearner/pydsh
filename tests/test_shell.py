"""One-shot command execution — Requirement 4, property 3.

Real subprocesses throughout. A mocked shell proves nothing about process
groups, and the process group is the whole point of the timeout path.
"""

from __future__ import annotations

import asyncio
import os
import signal

import pytest

from plugkit import Context

from pydsh.cancel import CancelSignal
from pydsh.capability import TERMINATED_EXIT_CODE, ShellService

pytestmark = pytest.mark.asyncio


async def mounted(**config) -> Context:
    ctx = Context()
    await ctx.plugin(ShellService, config)
    return ctx


def alive(pid: int) -> bool:
    """Whether a process still exists, asked of the OS rather than of us."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


# --------------------------------------------------------------------------- #
# Running (R4.1–R4.3, R4.6)
# --------------------------------------------------------------------------- #
async def test_a_command_reports_its_output_and_exit_code():
    root = await mounted()
    result = await root.shell.execute("echo hello")
    assert result["stdout"].strip() == "hello"
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert result["command"] == "echo hello"


async def test_a_failing_command_reports_its_code_and_stderr():
    root = await mounted()
    result = await root.shell.execute("echo oops >&2; exit 3")
    assert result["exit_code"] == 3
    assert "oops" in result["stderr"]


async def test_an_empty_command_is_rejected():
    root = await mounted()
    with pytest.raises(ValueError, match="command is required"):
        await root.shell.execute("   ")


async def test_the_working_directory_is_honoured(tmp_path):
    root = await mounted()
    result = await root.shell.execute("pwd", cwd=str(tmp_path))
    assert os.path.realpath(result["stdout"].strip()) == os.path.realpath(str(tmp_path))


async def test_caller_environment_is_merged_over_the_process_one():
    root = await mounted()
    result = await root.shell.execute("echo $PYDSH_TEST_VAR", env={"PYDSH_TEST_VAR": "set"})
    assert result["stdout"].strip() == "set"


async def test_invalid_utf8_output_is_replaced_not_fatal():
    root = await mounted()
    result = await root.shell.execute(r"printf 'good\xff\xfe'")
    assert "good" in result["stdout"]


# --------------------------------------------------------------------------- #
# Timeouts (R4.4) — property 3
# --------------------------------------------------------------------------- #
async def test_a_slow_command_times_out():
    root = await mounted()
    result = await root.shell.execute("sleep 5", timeout_ms=100)
    assert result["timed_out"] is True
    assert result["exit_code"] == TERMINATED_EXIT_CODE
    assert "stopped after" in result["stderr"]


async def test_a_timed_out_command_still_returns_what_it_produced():
    root = await mounted()
    result = await root.shell.execute("echo early; sleep 5", timeout_ms=300)
    assert result["timed_out"] is True
    assert "early" in result["stdout"]


async def test_a_timeout_kills_the_children_too(tmp_path):
    """Property 3 (I3) — the defect `proc.kill()` leaves behind.

    Killing only the shell leaves anything it started running. The child's pid
    is written to a file so the test can ask the OS directly, rather than
    trusting the return value of the thing under test.
    """
    pidfile = tmp_path / "child.pid"
    root = await mounted()

    result = await root.shell.execute(
        f"sleep 30 & echo $! > {pidfile}; sleep 30", timeout_ms=400
    )
    assert result["timed_out"] is True

    child_pid = int(pidfile.read_text().strip())
    await asyncio.sleep(0.3)  # let the signal land
    assert not alive(child_pid), "the command timed out but its child is still running"


async def test_a_command_that_finishes_in_time_is_not_marked(  ):
    root = await mounted()
    result = await root.shell.execute("echo quick", timeout_ms=5000)
    assert result["timed_out"] is False
    assert result["exit_code"] == 0


# --------------------------------------------------------------------------- #
# Cancellation (R4.5)
# --------------------------------------------------------------------------- #
async def test_a_cancel_signal_stops_the_command():
    root = await mounted()
    signal_ = CancelSignal()

    async def stop_soon():
        await asyncio.sleep(0.1)
        signal_.abort("the user stopped")

    asyncio.ensure_future(stop_soon())
    result = await root.shell.execute("sleep 5", signal=signal_)

    assert result["exit_code"] == TERMINATED_EXIT_CODE
    # A cancellation is not a timeout: a caller deciding whether to retry
    # needs to tell them apart.
    assert result["timed_out"] is False
    assert "cancelled" in result["stderr"]


async def test_an_already_aborted_signal_stops_it_immediately():
    root = await mounted()
    signal_ = CancelSignal()
    signal_.abort("already gone")
    result = await root.shell.execute("sleep 5", signal=signal_)
    assert result["exit_code"] == TERMINATED_EXIT_CODE


async def test_a_cancellation_also_takes_the_children(tmp_path):
    pidfile = tmp_path / "child.pid"
    root = await mounted()
    signal_ = CancelSignal()

    async def stop_soon():
        await asyncio.sleep(0.25)
        signal_.abort("stop")

    asyncio.ensure_future(stop_soon())
    await root.shell.execute(
        f"sleep 30 & echo $! > {pidfile}; sleep 30", signal=signal_
    )

    child_pid = int(pidfile.read_text().strip())
    await asyncio.sleep(0.3)
    assert not alive(child_pid)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
async def test_the_shell_can_be_configured():
    root = await mounted(shell="/bin/sh")
    assert root.shell.shell == "/bin/sh"
    assert (await root.shell.execute("echo ok"))["stdout"].strip() == "ok"
