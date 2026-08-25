"""A minimal MCP JSON-RPC client — the slice a tool bridge needs.

`initialize`, `tools/list`, `tools/call`, and the notification that says the
list changed. Nothing else of the protocol has a consumer here, and porting the
rest would be surface without a caller.

Two transports, behind one interface: a child process over newline-delimited
JSON-RPC, and streamable HTTP. The client depends on the interface and nothing
more — the reference type-switches on the HTTP transport and assigns a private
callback onto it, which makes the interface a lie and leaves a third transport
with no defined branch to fall into. An HTTP response *is* a JSON-RPC response
with an id, so the ordinary dispatch already handles it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Optional

#: The protocol version this client speaks.
PROTOCOL_VERSION = "2025-06-18"

#: How this client identifies itself in the handshake.
CLIENT_NAME = "pydsh-mcp-client"

#: Environment names that look like credentials, and are not passed to a child.
SENSITIVE_ENV_PATTERN = re.compile(r"KEY|PASSWORD|SECRET|TOKEN", re.I)

#: This project's own variables, also withheld: a child has no business
#: reading the harness's configuration.
OWN_ENV_PREFIX = "PYDSH_"

#: How long a request waits before it is declared lost.
DEFAULT_REQUEST_TIMEOUT = 60.0

#: The escalation a closing child gets: ask, then signal, then kill.
CLOSE_GRACE_SECONDS = 3.0
TERMINATE_GRACE_SECONDS = 2.0

#: What a POST may take in total.
HTTP_TIMEOUT_SECONDS = 300.0

#: Bytes of an error body quoted back.
ERROR_BODY_CHARS = 200

#: How long the notification stream waits before reconnecting.
NOTIFY_RETRY_SECONDS = 1.0


class McpError(Exception):
    """A transport or JSON-RPC failure, carrying the protocol code when there is one."""

    def __init__(
        self, message: str, code: Optional[int] = None, cause: Any = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.cause = cause


def scrubbed_parent_env() -> dict:
    """The parent environment minus credential-shaped names and our own.

    **The base for a child's environment, never an overlay.** The reference
    copies the whole environment and then updates it with this subset, which
    removes nothing — every key the scrub dropped is already in the copy and
    stays. A security control that does nothing is worse than none, because it
    stops anyone looking further.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not SENSITIVE_ENV_PATTERN.search(key)
        and not key.upper().startswith(OWN_ENV_PREFIX)
    }


# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #
class Transport(ABC):
    """Sends JSON-RPC messages and hands received ones to a callback."""

    @abstractmethod
    async def start(self, on_message: Callable[[dict], None]) -> None:
        """Bring the transport up. Every message arrives through ``on_message``."""

    @abstractmethod
    async def send(self, payload: dict) -> None:
        """Send one JSON-RPC message."""

    @abstractmethod
    async def close(self) -> None:
        """Shut down. Idempotent."""


class StdioTransport(Transport):
    """A child process speaking newline-delimited JSON-RPC."""

    def __init__(
        self,
        command: str,
        args: Optional[list] = None,
        env: Optional[dict] = None,
        cwd: Optional[str] = None,
    ) -> None:
        self.command = command
        self.args = list(args or ())
        self.env = dict(env or {})
        self.cwd = cwd or None
        self._process: Optional[Any] = None
        self._reader: Optional[asyncio.Task] = None

    def child_env(self) -> dict:
        """What the child sees: the scrub, plus what config adds (I3)."""
        environment = scrubbed_parent_env()
        environment.update(self.env)
        return environment

    async def start(self, on_message: Callable[[dict], None]) -> None:
        self._process = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=self.child_env(),
            cwd=self.cwd,
            # Its own process group, so closing this connection ends what the
            # child started rather than orphaning its children (I4).
            start_new_session=True,
        )
        self._reader = asyncio.ensure_future(self._read_loop(on_message))

    async def _read_loop(self, on_message: Callable[[dict], None]) -> None:
        stdout = self._process.stdout
        while True:
            line = await stdout.readline()
            if not line:
                return  # EOF: the child is gone
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                # Servers log to stdout. A stray line is noise, not a fault —
                # killing the connection over one would make this brittle
                # against something entirely harmless.
                continue
            if isinstance(payload, dict):
                on_message(payload)

    async def send(self, payload: dict) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise McpError("the MCP child process is not running")
        try:
            process.stdin.write(
                (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            )
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise McpError("the MCP child process closed its input", cause=exc) from exc

    async def close(self) -> None:
        process, self._process = self._process, None
        reader, self._reader = self._reader, None
        if process is not None:
            await self._stop(process)
        if reader is not None:
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _stop(self, process: Any) -> None:
        """Close stdin, then escalate to the process **group** (I4)."""
        try:
            if process.stdin is not None:
                process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=CLOSE_GRACE_SECONDS)
            return
        except asyncio.TimeoutError:
            pass

        # Signalling the group, not the process: a server that spawned helpers
        # would otherwise leave them running with nothing to talk to.
        self._signal_group(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
        self._signal_group(process, signal.SIGKILL)
        try:
            await process.wait()
        except (ProcessLookupError, asyncio.CancelledError):
            pass

    @staticmethod
    def _signal_group(process: Any, sig: int) -> None:
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            # Already gone, or never got its own group. Fall back to the
            # process itself rather than giving up on stopping it.
            try:
                process.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass


class StreamableHttpTransport(Transport):
    """POSTs requests, and reads notifications from a background GET stream."""

    def __init__(self, url: str, headers: Optional[dict] = None) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.session_id: Optional[str] = None
        self._on_message: Optional[Callable[[dict], None]] = None
        self._notify: Optional[asyncio.Task] = None
        self._closed = False

    async def start(self, on_message: Callable[[dict], None]) -> None:
        # Only recorded. The notification stream needs the session id, which
        # does not exist until `initialize` has been answered.
        self._on_message = on_message

    def _ensure_notifications(self) -> None:
        if self._notify is not None or self._on_message is None or self.session_id is None:
            return
        self._notify = asyncio.ensure_future(self._notification_loop(self._on_message))

    async def _notification_loop(self, on_message: Callable[[dict], None]) -> None:
        while not self._closed:
            try:
                httpx = _httpx()
                headers = {"accept": "text/event-stream", "mcp-session-id": self.session_id}
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", self.url, headers=headers) as response:
                        if response.status_code >= 400:
                            raise McpError(
                                f"the MCP notification stream failed "
                                f"(HTTP {response.status_code})"
                            )
                        async for line in response.aiter_lines():
                            if self._closed:
                                return
                            payload = _sse_payload(line)
                            # A notification has no id. Anything with one is a
                            # response, which does not belong on this stream.
                            if payload is not None and "id" not in payload:
                                on_message(payload)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 - a dropped stream is reconnected
                if self._closed:
                    return
                await asyncio.sleep(NOTIFY_RETRY_SECONDS)

    async def send(self, payload: dict) -> None:
        httpx = _httpx()
        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            **self.headers,
        }
        if self.session_id is not None:
            headers["mcp-session-id"] = self.session_id

        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                async with client.stream(
                    "POST", self.url, json=payload, headers=headers
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread())[:ERROR_BODY_CHARS]
                        raise McpError(
                            f"the MCP request failed (HTTP {response.status_code}): {body!r}"
                        )
                    self.session_id = (
                        response.headers.get("mcp-session-id") or self.session_id
                    )
                    self._ensure_notifications()
                    await self._read_response(response)
        except McpError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise McpError(
                f"the MCP request {payload.get('method')!r} could not be sent",
                cause=exc,
            ) from exc

    async def _read_response(self, response: Any) -> None:
        """Deliver the response through ``on_message``, whatever framed it."""
        if "text/event-stream" in (response.headers.get("content-type") or ""):
            async for line in response.aiter_lines():
                payload = _sse_payload(line)
                if payload is not None:
                    self._deliver(payload)
                    return
            raise McpError("the MCP SSE response ended before any message")

        body = await response.aread()
        # A notification is answered with 202 and no body. Parsing that would
        # report a malformed response where there is simply nothing to say.
        if not body or response.status_code == 202:
            return
        try:
            self._deliver(json.loads(body))
        except json.JSONDecodeError as exc:
            raise McpError(f"the MCP response is not JSON: {body[:120]!r}") from exc

    def _deliver(self, payload: Any) -> None:
        if isinstance(payload, dict) and self._on_message is not None:
            self._on_message(payload)

    async def close(self) -> None:
        self._closed = True
        notify, self._notify = self._notify, None
        if notify is not None:
            notify.cancel()
            try:
                await notify
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


def _sse_payload(line: str) -> Optional[dict]:
    """One SSE `data:` line as JSON, or ``None`` for anything else."""
    line = (line or "").strip()
    if not line.startswith("data:"):
        return None
    data = line[len("data:"):].strip()
    if not data or data == "[DONE]":
        return None
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _httpx() -> Any:
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise McpError(
            "the HTTP MCP transport needs httpx; install pydsh[http]"
        ) from exc
    return httpx


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #
class McpClient:
    """Request/response matching, notification dispatch, and the tool calls."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self.server_info: Optional[dict] = None
        self._next_id = 0
        self._pending: dict[Any, asyncio.Future] = {}
        self._handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}
        self._closed = False

    # -- lifecycle --------------------------------------------------------- #
    async def connect(self) -> dict:
        """Handshake: `initialize`, then the notification that confirms it."""
        await self.transport.start(self._on_message)
        result = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": _version()},
            },
        )
        self.server_info = (result or {}).get("serverInfo")
        # Only after this may the server send anything of its own.
        await self.notify("notifications/initialized")
        return result or {}

    async def close(self, reason: str = "the MCP client was closed") -> None:
        """Close the transport and fail every request in flight. Idempotent."""
        if self._closed:
            return
        self._closed = True
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                # Failed with a reason rather than cancelled: a bare
                # CancelledError reaching a caller looks like *their* task was
                # cancelled, which sends them looking in the wrong place.
                future.set_exception(McpError(reason))
        await self.transport.close()

    @property
    def closed(self) -> bool:
        return self._closed

    # -- JSON-RPC ---------------------------------------------------------- #
    def on_notification(
        self, method: str, handler: Callable[[dict], Awaitable[None]]
    ) -> None:
        self._handlers[method] = handler

    async def request(
        self,
        method: str,
        params: Optional[dict] = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> Any:
        """Send a request and wait for its answer.

        :raises McpError: the client is closed, the server answered with an
            error, or nothing came back in time.
        """
        if self._closed:
            raise McpError("the MCP client is closed")
        self._next_id += 1
        message_id = self._next_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id, "method": method}
        if params is not None:
            payload["params"] = params

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await self.transport.send(payload)
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise McpError(f"the MCP request {method!r} timed out after {timeout}s") from exc
        finally:
            self._pending.pop(message_id, None)

    async def notify(self, method: str, params: Optional[dict] = None) -> None:
        """Send a notification: no id, no answer."""
        if self._closed:
            raise McpError("the MCP client is closed")
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self.transport.send(payload)

    # -- dispatch ---------------------------------------------------------- #
    def _on_message(self, payload: dict) -> None:
        """One inbound message. An id makes it a response; no id, a notification."""
        if "id" in payload and payload["id"] is not None:
            self._resolve(payload["id"], payload)
            return
        handler = self._handlers.get(payload.get("method"))
        if handler is not None:
            task = asyncio.ensure_future(handler(payload.get("params") or {}))
            # Held so a failing handler surfaces as its own error rather than
            # asyncio's "exception was never retrieved" at collection time.
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    def _resolve(self, message_id: Any, payload: dict) -> None:
        # Matched by the id as sent. JSON-RPC ids may be strings, and the
        # reference coerces with int(), which raises on one.
        future = self._pending.get(message_id)
        if future is None or future.done():
            return
        error = payload.get("error")
        if error is not None:
            future.set_exception(
                McpError(error.get("message", "MCP error"), code=error.get("code"))
            )
        else:
            future.set_result(payload.get("result"))

    # -- tools ------------------------------------------------------------- #
    async def list_tools(
        self, cursor: Optional[str] = None
    ) -> tuple[list, Optional[str]]:
        """One page of the server's tools, and the cursor for the next."""
        params = {"cursor": cursor} if cursor is not None else None
        result = await self.request("tools/list", params) or {}
        return list(result.get("tools") or ()), result.get("nextCursor")

    async def call_tool(
        self, name: str, arguments: Optional[dict] = None, timeout: float = DEFAULT_REQUEST_TIMEOUT
    ) -> dict:
        """Call a tool by its **raw** name — the bridge owns the public one."""
        return await self.request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout=timeout,
        ) or {}


def _version() -> str:
    from .. import __version__

    return __version__


__all__ = [
    "McpClient",
    "McpError",
    "Transport",
    "StdioTransport",
    "StreamableHttpTransport",
    "scrubbed_parent_env",
    "PROTOCOL_VERSION",
    "CLIENT_NAME",
    "SENSITIVE_ENV_PATTERN",
    "OWN_ENV_PREFIX",
    "DEFAULT_REQUEST_TIMEOUT",
    "CLOSE_GRACE_SECONDS",
    "TERMINATE_GRACE_SECONDS",
]
