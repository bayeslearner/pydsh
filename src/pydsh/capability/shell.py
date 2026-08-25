"""``ctx.shell`` — run one command and report what it did.

Capability, not policy. This service runs what it is given; deciding *whether*
a command may run belongs to the tools pipeline, where a guard or an approver
can see the caller and say no. Splitting a half-policy across both would give
the appearance of safety while the real decision has nowhere to live.

The part that needs care is the timeout, and it is the part the reference gets
wrong. Killing the process is not the same as stopping the work: a shell
command that started children leaves them running when only the leader dies. So
the command is spawned into its own process group and the whole group is
signalled — otherwise "it timed out" does not mean it stopped, and a
long-running harness quietly accumulates orphans.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal as signals
from typing import Any, Optional

from plugkit import Service

from ..cancel import CancelSignal
from .timeout import TimeoutReason, deadline, timeout_of

logger = logging.getLogger("pydsh.shell")

#: Seconds a signalled process group is given to exit before it is killed.
GRACE_SECONDS = 0.25

#: The exit code reported for a command that was stopped rather than finished.
TERMINATED_EXIT_CODE = -1

#: The abort code a shell timeout carries, so a caller can recognise it.
TIMEOUT_CODE = "shell-timeout"


class ShellService(Service):
    """Provides ``ctx.shell``."""

    provide = "shell"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        self._shell = config.get("shell") or self._default_shell()

    @staticmethod
    def _default_shell() -> str:
        """The user's shell, or a POSIX fallback."""
        return os.environ.get("SHELL") or "/bin/sh"

    @property
    def shell(self) -> str:
        return self._shell

    async def execute(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout_ms: Optional[int] = None,
        env: Optional[dict] = None,
        signal: Optional[CancelSignal] = None,
    ) -> dict:
        """Run a command to completion, a timeout, or a cancellation.

        Returns the command, its output, its exit code, and whether it was cut
        short. A stopped command reports ``TERMINATED_EXIT_CODE`` and whatever
        it had already produced — partial output is more useful than none.
        """
        if not command.strip():
            raise ValueError("a command is required")

        process_env = {**os.environ, **(env or {})}
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=process_env,
            executable=self._shell,
            # Its own process group, so a timeout can signal everything the
            # command started rather than only the shell that started it.
            start_new_session=True,
        )

        bound = deadline(signal, timeout_ms or 0, TIMEOUT_CODE)
        try:
            return await self._await_or_stop(process, command, bound.signal)
        finally:
            bound.dispose()

    async def _await_or_stop(
        self, process: Any, command: str, signal: CancelSignal
    ) -> dict:
        """Wait for the command, stopping it if the signal fires first."""
        communicate = asyncio.ensure_future(process.communicate())
        stopped = asyncio.get_running_loop().create_future()

        def on_abort() -> None:
            if not stopped.done():
                stopped.set_result(signal.reason)

        if signal.aborted:
            on_abort()
        detach = signal.add_listener(on_abort)

        try:
            done, _ = await asyncio.wait(
                {communicate, stopped}, return_when=asyncio.FIRST_COMPLETED
            )
            if communicate in done:
                out, err = communicate.result()
                return self._result(command, out, err, process.returncode, False)

            reason = stopped.result()
            await self._stop_group(process)
            out, err = await communicate
            timed_out = timeout_of(reason) is not None
            note = (
                f"\n[command stopped after {reason.timeout_ms}ms]"
                if isinstance(reason, TimeoutReason)
                else "\n[command cancelled]"
            )
            return self._result(
                command, out, (err or b"") + note.encode(), TERMINATED_EXIT_CODE, timed_out
            )
        finally:
            detach()
            if not communicate.done():
                communicate.cancel()

    async def _stop_group(self, process: Any) -> None:
        """Signal the command's whole process group, escalating if it lingers.

        ``killpg`` rather than ``process.kill()``: the latter signals only the
        shell, and anything it started keeps running.
        """
        try:
            group = os.getpgid(process.pid)
        except (ProcessLookupError, PermissionError, OSError):
            return  # already gone, or not ours to signal

        for sig in (signals.SIGTERM, signals.SIGKILL):
            try:
                os.killpg(group, sig)
            except (ProcessLookupError, PermissionError, OSError):
                return
            try:
                await asyncio.wait_for(process.wait(), timeout=GRACE_SECONDS)
                return
            except asyncio.TimeoutError:
                continue  # still alive: escalate
        logger.warning("shell: process group %s survived SIGKILL", group)

    @staticmethod
    def _result(
        command: str, out: bytes, err: bytes, exit_code: Any, timed_out: bool
    ) -> dict:
        return {
            "command": command,
            "stdout": (out or b"").decode("utf-8", errors="replace"),
            "stderr": (err or b"").decode("utf-8", errors="replace"),
            "exit_code": exit_code,
            "timed_out": timed_out,
        }


__all__ = ["ShellService", "GRACE_SECONDS", "TERMINATED_EXIT_CODE", "TIMEOUT_CODE"]
