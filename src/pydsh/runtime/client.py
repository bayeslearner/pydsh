"""The runtime client — the `Harness` shape, backed by another process.

::

    async with RuntimeClient(provider="openai", model="gpt-4o") as client:
        result = await client.session("my-chat").run("what changed today?")

Deliberately the same shape as the in-process harness, because the *choice*
between them is a deployment decision and should not be a rewrite. What differs
is what a caller gains: events arrive as notifications while the turn runs, so
a client can stream a conversation it is not hosting.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .protocol import JsonRpcError, JsonRpcTransport, Reader, TransportClosed, Writer
from .server import EVENT_NOTIFICATION, STATUS_NOTIFICATION

logger = logging.getLogger("pydsh.runtime")

#: How a client names a session the caller did not.
SESSION_ID_PREFIX = "session-"

#: How long the handshake may take before the runtime is declared unreachable.
DEFAULT_HANDSHAKE_TIMEOUT = 30.0

#: How long a child gets to exit after its input closes.
CHILD_GRACE_SECONDS = 3.0

#: The module a spawned runtime runs.
RUNTIME_MODULE = "pydsh.runtime"


@dataclass
class RemoteRunResult:
    """What one remote turn produced."""

    session_id: str
    final_response: str
    events: list = field(default_factory=list)
    event_count: int = 0


class RemoteSession:
    """A handle on one conversation inside the runtime."""

    def __init__(self, client: "RuntimeClient", session_id: str) -> None:
        self.client = client
        self.id = session_id

    async def run(self, text: Any, timeout: Optional[float] = None) -> RemoteRunResult:
        """Deliver a message and wait for the turn."""
        seen: list = []
        release = self.client.on_event(
            lambda payload: seen.append(payload)
            if payload.get("session_id") == self.id
            else None
        )
        try:
            answer = await self.client.request(
                "session/run", {"session_id": self.id, "content": text}, timeout
            )
        finally:
            release()
        return RemoteRunResult(
            session_id=answer.get("session_id", self.id),
            final_response=answer.get("final_response", ""),
            events=seen,
            event_count=answer.get("event_count", len(seen)),
        )

    async def send(self, text: Any) -> dict:
        """Deliver a message without waiting for the turn."""
        return await self.client.request(
            "session/prompt", {"session_id": self.id, "content": text}
        )


class RuntimeClient:
    """Talks to a runtime, spawning one unless given a transport."""

    def __init__(
        self,
        *,
        provider: str = "",
        model: str = "",
        max_tokens: Optional[int] = None,
        transport: Optional[JsonRpcTransport] = None,
        command: Optional[list] = None,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
    ) -> None:
        self.provider = provider
        self.model = model
        self.max_tokens = max_tokens
        self.command = list(command or [sys.executable, "-u", "-m", RUNTIME_MODULE])
        self.cwd = cwd
        self.env = env
        self.handshake_timeout = handshake_timeout
        self.transport = transport
        self.server_info: dict = {}
        self._owns_transport = transport is None
        self._process: Optional[Any] = None
        self._event_handlers: list = []
        self._status_handlers: list = []
        self._started = False
        self._closed = False

    # -- lifecycle --------------------------------------------------------- #
    async def start(self) -> "RuntimeClient":
        """Connect and handshake. Idempotent."""
        if self._started:
            return self
        if self._closed:
            raise TransportClosed("this client has been closed")

        if self.transport is None:
            self.transport = await self._spawn()
        self.transport.on_notification(self._on_notification)
        self.transport.start()

        self.server_info = await self.transport.request(
            "initialize",
            {
                "provider": self.provider,
                "model": self.model,
                "max_tokens": self.max_tokens,
                "cwd": self.cwd or os.getcwd(),
            },
            timeout=self.handshake_timeout,
        )
        self._started = True
        return self

    async def _spawn(self) -> JsonRpcTransport:
        process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # Not inherited and not captured into the frame stream: the child's
            # own output has nowhere useful to go here, and on stdout it would
            # be a line this client has to parse.
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self.cwd,
            env=self.env,
            start_new_session=True,
        )
        self._process = process

        async def read() -> Optional[str]:
            line = await process.stdout.readline()
            return line.decode("utf-8", "replace") if line else None

        def write(line: str) -> None:
            process.stdin.write((line + "\n").encode("utf-8"))

        return JsonRpcTransport(read, write)

    async def close(self) -> None:
        """Shut the runtime down and stop the child. Idempotent."""
        if self._closed:
            return
        self._closed = True
        transport, self.transport = self.transport, None
        if transport is not None and self._started:
            try:
                await transport.request("shutdown", timeout=CHILD_GRACE_SECONDS)
            except (TransportClosed, JsonRpcError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass  # the runtime may already be gone; we are closing anyway
        if transport is not None and self._owns_transport:
            await transport.close("the client closed the connection")
        await self._stop_child()
        self._started = False

    async def _stop_child(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=CHILD_GRACE_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
        # The group, not the process: a runtime that spawned an MCP server
        # would otherwise leave it running with nothing to talk to.
        import signal

        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except (ProcessLookupError, OSError):
                pass
        try:
            await process.wait()
        except (ProcessLookupError, asyncio.CancelledError):
            pass

    async def __aenter__(self) -> "RuntimeClient":
        return await self.start()

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    # -- talking to it ----------------------------------------------------- #
    async def request(
        self, method: str, params: Optional[dict] = None, timeout: Optional[float] = None
    ) -> Any:
        await self.start()
        transport = self.transport
        if transport is None:
            raise TransportClosed("this client has been closed")
        if self._process is not None and self._process.returncode is not None:
            # Asked before waiting rather than after: a dead runtime will never
            # answer, and the caller should be told that instead of the timeout
            # they would otherwise wait out.
            raise TransportClosed(
                f"the runtime exited with code {self._process.returncode}"
            )
        return await transport.request(method, params, timeout)

    def session(self, session_id: Optional[str] = None) -> RemoteSession:
        return RemoteSession(
            self, session_id or f"{SESSION_ID_PREFIX}{uuid.uuid4().hex[:12]}"
        )

    # -- notifications ----------------------------------------------------- #
    def on_event(self, handler: Callable[[dict], Any]) -> Callable[[], None]:
        """Observe session events as they arrive. Returns a disposer."""
        self._event_handlers.append(handler)
        return lambda: _remove(self._event_handlers, handler)

    def on_status(self, handler: Callable[[dict], Any]) -> Callable[[], None]:
        self._status_handlers.append(handler)
        return lambda: _remove(self._status_handlers, handler)

    def _on_notification(self, method: str, params: dict) -> None:
        handlers = (
            self._event_handlers
            if method == EVENT_NOTIFICATION
            else self._status_handlers
            if method == STATUS_NOTIFICATION
            else ()
        )
        for handler in list(handlers):
            try:
                handler(params)
            except Exception as error:  # noqa: BLE001 - an observer is not a participant
                logger.warning("runtime client: an event handler failed: %s", error)


def _remove(handlers: list, handler: Any) -> None:
    try:
        handlers.remove(handler)
    except ValueError:
        pass


__all__ = [
    "RuntimeClient",
    "RemoteSession",
    "RemoteRunResult",
    "SESSION_ID_PREFIX",
    "DEFAULT_HANDSHAKE_TIMEOUT",
    "CHILD_GRACE_SECONDS",
    "RUNTIME_MODULE",
]
