"""``ctx.terminal`` — a shell that stays alive between calls.

The opposite shape to :mod:`~pydsh.capability.shell`. One-shot execution has no
state: every command starts where the last one did not leave off. A terminal
keeps its working directory, its environment, its shell variables — so a model
can ``cd`` somewhere and have the next command land there.

That state is the whole feature and the whole risk. An interactive shell is a
live process holding pipes, and one that outlives the harness is a leak with a
shell attached. So the service owns every session it spawns and closes them all
when it unmounts.

Reading is bounded by a **sentinel**, not by silence. Each command is followed
by an echo of a unique marker, and reading stops when that marker arrives.
Waiting for output to go quiet cannot tell "produced nothing" from "has not
started yet", so a `cd` would pay the whole timeout every time.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal as signals
import uuid
from typing import Any, Optional

from plugkit import Service

logger = logging.getLogger("pydsh.terminal")

#: How long a single `send` may collect output before giving up on its marker.
#: Only reached by a command that never finishes — a normal one returns as soon
#: as its marker arrives.
DEFAULT_WAIT_MS = 4000

#: Seconds a closing session is given to exit before it is killed.
GRACE_SECONDS = 0.25


class TerminalClosedError(RuntimeError):
    """The session has been closed, or its shell exited on its own."""


class TerminalSession:
    """One live shell and the pipes into it."""

    def __init__(self, id: str, process: Any, cwd: Optional[str] = None) -> None:
        self.id = id
        self.cwd = cwd
        self._process = process
        self._buffer: list[str] = []
        self._closed = False

    @property
    def closed(self) -> bool:
        """Whether this session is finished — closed by us, or exited itself."""
        return self._closed or self._process.returncode is not None

    def _assert_open(self) -> None:
        if self.closed:
            raise TerminalClosedError(f"terminal session {self.id!r} is closed")

    async def send(self, command: str, wait_ms: int = DEFAULT_WAIT_MS) -> str:
        """Run a command and return its output, without waiting for a silence.

        A **sentinel** rather than a settle: the command is followed by an echo
        of a unique marker, and reading stops as soon as that marker arrives.

        Waiting for output to go quiet cannot tell "this command produced
        nothing" from "this command has not started yet", so a `cd` — which
        prints nothing at all — pays the entire timeout every time. The marker
        removes the ambiguity: its arrival *is* the end of the command.
        """
        self._assert_open()
        marker = f"__pydsh_done_{uuid.uuid4().hex[:12]}__"
        self._process.stdin.write(f"{command}\n echo {marker}\n".encode())
        await self._process.stdin.drain()
        return await self._read_until(marker, wait_ms)

    async def _read_until(self, marker: str, wait_ms: int) -> str:
        """Collect output up to the marker, or until the wait runs out."""
        loop = asyncio.get_running_loop()
        give_up_at = loop.time() + wait_ms / 1000.0
        collected = ""

        while loop.time() < give_up_at:
            remaining = give_up_at - loop.time()
            try:
                data = await asyncio.wait_for(
                    self._process.stdout.read(4096), timeout=remaining
                )
            except asyncio.TimeoutError:
                break
            if not data:
                self._closed = True  # the shell ended
                break
            collected += data.decode("utf-8", errors="replace")
            if marker in collected:
                collected = collected.split(marker, 1)[0]
                break

        # The echoed marker command can itself appear when the shell echoes
        # input; drop that line rather than handing it back as output.
        text = "\n".join(
            line for line in collected.splitlines() if marker not in line
        )
        self._buffer.append(text)
        return text

    async def read_available(self) -> str:
        """Whatever has arrived since the last read, without waiting for more."""
        self._assert_open()
        try:
            data = await asyncio.wait_for(self._process.stdout.read(4096), timeout=0.01)
        except asyncio.TimeoutError:
            return ""
        return data.decode("utf-8", errors="replace")

    async def close(self) -> None:
        """End the session. Idempotent, and it really does end the process."""
        if self._closed and self._process.returncode is not None:
            return
        self._closed = True
        if self._process.returncode is not None:
            return

        try:
            self._process.stdin.close()
        except (OSError, AttributeError):
            pass

        # Its own group, so anything the session started goes with it.
        try:
            group = os.getpgid(self._process.pid)
        except (ProcessLookupError, PermissionError, OSError):
            return
        for sig in (signals.SIGTERM, signals.SIGKILL):
            try:
                os.killpg(group, sig)
            except (ProcessLookupError, PermissionError, OSError):
                return
            try:
                await asyncio.wait_for(self._process.wait(), timeout=GRACE_SECONDS)
                return
            except asyncio.TimeoutError:
                continue
        logger.warning("terminal %r: process group survived SIGKILL", self.id)

    def snapshot(self) -> dict:
        """What this session is, for a caller listing them."""
        return {"id": self.id, "cwd": self.cwd, "closed": self.closed}


class TerminalService(Service):
    """Provides ``ctx.terminal``."""

    provide = "terminal"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        self._shell = config.get("shell") or os.environ.get("SHELL") or "/bin/sh"
        self._sessions: dict[str, TerminalSession] = {}
        # A live shell that outlives the harness is a leak with a shell
        # attached, so unmounting takes them all with it.
        ctx.effect(lambda: self._abandon)

    def _abandon(self) -> None:
        """Unmount: stop every session, synchronously.

        Teardown cannot await, so this signals the process groups directly
        rather than going through the async close.
        """
        for session in list(self._sessions.values()):
            try:
                os.killpg(os.getpgid(session._process.pid), signals.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                continue
        self._sessions.clear()

    async def spawn(
        self, id: Optional[str] = None, cwd: Optional[str] = None
    ) -> TerminalSession:
        """Start a shell that will stay alive until it is closed."""
        session_id = id or uuid.uuid4().hex[:12]
        if session_id in self._sessions and not self._sessions[session_id].closed:
            raise ValueError(f"terminal session {session_id!r} is already open")

        process = await asyncio.create_subprocess_exec(
            self._shell,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            start_new_session=True,
        )
        session = TerminalSession(session_id, process, cwd)
        self._sessions[session_id] = session
        return session

    def get(self, id: str) -> TerminalSession:
        session = self._sessions.get(id)
        if session is None:
            known = ", ".join(sorted(self._sessions)) or "none"
            raise KeyError(f"no terminal session {id!r} (open: {known})")
        return session

    def list(self) -> list[dict]:
        return [s.snapshot() for s in self._sessions.values()]

    async def close(self, id: str) -> None:
        session = self._sessions.pop(id, None)
        if session is not None:
            await session.close()

    async def close_all(self) -> None:
        for id in list(self._sessions):
            await self.close(id)


__all__ = [
    "TerminalService",
    "TerminalSession",
    "TerminalClosedError",
    "DEFAULT_WAIT_MS",
]
