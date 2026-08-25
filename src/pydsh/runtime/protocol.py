"""Newline-delimited JSON-RPC 2.0 — one frame per line, both directions.

Two things here are the whole design, and the reference gets both wrong.

**Requests are dispatched as their own tasks.** The read loop is also how
*responses* arrive, so a handler awaited inside it cannot await anything that
needs an inbound frame — including a call back to the peer. That is not a
slowdown; it is a deadlock against the loop that would deliver the answer, and
it only shows up once someone uses bidirectional calls.

**The stdin hand-off goes through the loop.** Reading stdin needs a thread, and
`asyncio.Queue` is not thread-safe. `loop.call_soon_threadsafe` is the correct
call; `put_nowait` from a foreign thread fails rarely, unpredictably, and looks
exactly like a dropped frame.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import uuid
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("pydsh.runtime")

#: How a request id is spelled, so one is recognisable in a log.
REQUEST_ID_PREFIX = "req_"

#: The JSON-RPC codes this endpoint answers with.
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603

#: ``() -> str | None`` — the next line, or ``None`` at end of input.
Reader = Callable[[], Awaitable[Optional[str]]]
#: ``(str) -> None`` — write one line, including its newline.
Writer = Callable[[str], None]


class JsonRpcError(Exception):
    """The peer answered with an error frame."""

    def __init__(self, message: str, code: Optional[int] = None, data: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data


class TransportClosed(Exception):
    """The connection is gone, and whatever was in flight is not coming back."""


class MethodNotFound(Exception):
    """A handler saying "not mine", which is a different answer from "it broke".

    Defined here rather than in the server so the transport can answer with the
    right code. Without it, a request for a method nobody serves comes back as
    an internal error, and a client cannot tell "you asked for the wrong thing"
    from "I fell over".
    """


# --------------------------------------------------------------------------- #
# Readers and writers
# --------------------------------------------------------------------------- #
def stdin_reader() -> Reader:
    """Read lines from stdin on a daemon thread, handed over through the loop.

    A daemon thread rather than an executor: a thread-pool worker is not a
    daemon, so a process that has shut down but still holds stdin open blocks
    its own exit inside `readline`.
    """
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def pump() -> None:
        try:
            while True:
                line = sys.stdin.readline()
                # Through the loop (I2). `asyncio.Queue` is not thread-safe, and
                # calling `put_nowait` from here corrupts it in a way that
                # presents as a dropped frame.
                loop.call_soon_threadsafe(queue.put_nowait, line)
                if not line:
                    return  # end of input
        except Exception:  # noqa: BLE001 - a read failure is end of input
            loop.call_soon_threadsafe(queue.put_nowait, "")

    threading.Thread(target=pump, daemon=True, name="pydsh-stdin").start()

    async def read() -> Optional[str]:
        line = await queue.get()
        return line if line else None

    return read


def stdout_writer() -> Writer:
    """Write frames to stdout, flushed.

    Nothing else in the process may write there: a stray `print` becomes a line
    a peer's parser has to deal with.
    """

    def write(line: str) -> None:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    return write


def pipe() -> tuple[Reader, Writer]:
    """An in-memory pipe: what one end writes, the other end reads."""
    queue: asyncio.Queue = asyncio.Queue()

    async def read() -> Optional[str]:
        return await queue.get()

    def write(line: str) -> None:
        queue.put_nowait(line)

    return read, write


def duplex() -> tuple[tuple[Reader, Writer], tuple[Reader, Writer]]:
    """Two ends of one connection, crossed over."""
    read_a, write_a = pipe()
    read_b, write_b = pipe()
    return (read_a, write_b), (read_b, write_a)


# --------------------------------------------------------------------------- #
# The transport
# --------------------------------------------------------------------------- #
class JsonRpcTransport:
    """One endpoint: sends requests and notifications, serves inbound ones."""

    def __init__(
        self, reader: Optional[Reader] = None, writer: Optional[Writer] = None
    ) -> None:
        self._reader = reader or stdin_reader()
        self._writer = writer or stdout_writer()
        self._on_request: Optional[Callable[[str, dict], Awaitable[Any]]] = None
        self._on_notification: Optional[Callable[[str, dict], Any]] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._inflight: set = set()
        self._task: Optional[asyncio.Task] = None
        self._closed = False
        self.eof = False

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        """Begin reading. Idempotent."""
        if self._task is None and not self._closed:
            self._task = asyncio.ensure_future(self._read_loop())

    async def close(self, reason: str = "the connection closed") -> None:
        """Stop reading and fail everything in flight (I4). Idempotent."""
        if self._closed:
            return
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for handler in list(self._inflight):
            handler.cancel()
        self._inflight.clear()
        self._fail_pending(TransportClosed(reason))

    @property
    def closed(self) -> bool:
        return self._closed

    # -- handlers ---------------------------------------------------------- #
    def on_request(self, handler: Callable[[str, dict], Awaitable[Any]]) -> None:
        self._on_request = handler

    def on_notification(self, handler: Callable[[str, dict], Any]) -> None:
        self._on_notification = handler

    # -- sending ----------------------------------------------------------- #
    async def request(
        self,
        method: str,
        params: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Send a request and wait for its answer."""
        if self._closed:
            raise TransportClosed("the connection is closed")
        request_id = f"{REQUEST_ID_PREFIX}{uuid.uuid4().hex}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params or {},
                }
            )
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        """Send a notification. Never raises at the caller (I3).

        Contained on purpose: a notification is usually emitted from an
        observer — a session append, an agent status change — and the contract
        there is that an observer cannot turn a committed fact into a failure.
        """
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        try:
            self._write(message)
        except Exception as error:  # noqa: BLE001
            logger.warning("runtime: notification %r could not be sent: %s", method, error)

    def _write(self, message: dict) -> None:
        try:
            self._writer(json.dumps(message, ensure_ascii=False))
        except Exception as error:  # noqa: BLE001 - the connection is gone
            self._fail_pending(TransportClosed(f"the connection failed on write: {error}"))
            raise TransportClosed(f"the connection failed on write: {error}") from error

    # -- reading ----------------------------------------------------------- #
    async def _read_loop(self) -> None:
        while not self._closed:
            try:
                line = await self._reader()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a read failure is end of input
                break
            if line is None:
                self.eof = True
                break
            line = line.strip()
            if not line:
                continue
            self._handle(line)
        if not self._closed:
            self._fail_pending(TransportClosed("the peer closed the connection"))

    def _handle(self, line: str) -> None:
        frame = _parse(line)
        if frame is None:
            # One unparseable line is noise — a stray `print` from a plugin, a
            # banner from a wrapper — not a reason to drop a working connection.
            return
        frame_id = frame.get("id")
        method = frame.get("method")
        params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
        has_id = isinstance(frame_id, (str, int)) and not isinstance(frame_id, bool)

        if has_id and isinstance(method, str):
            # Its own task (I1). Awaited here, a handler could not await
            # anything needing an inbound frame — the loop that delivers it is
            # this one.
            task = asyncio.ensure_future(self._serve(frame_id, method, params))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
            return
        if has_id:
            self._resolve(frame_id, frame)
            return
        if isinstance(method, str) and self._on_notification is not None:
            try:
                result = self._on_notification(method, params)
                if hasattr(result, "__await__"):
                    asyncio.ensure_future(result)
            except Exception as error:  # noqa: BLE001
                logger.warning("runtime: notification handler failed: %s", error)

    async def _serve(self, request_id: Any, method: str, params: dict) -> None:
        handler = self._on_request
        if handler is None:
            self._error(request_id, METHOD_NOT_FOUND, f"no method {method!r}")
            return
        try:
            result = await handler(method, params)
        except asyncio.CancelledError:
            raise
        except MethodNotFound as error:
            self._error(request_id, METHOD_NOT_FOUND, str(error) or f"no method {method!r}")
            return
        except Exception as error:  # noqa: BLE001 - a failure is an answer
            # The message is carried across deliberately: a client that cannot
            # see why has nothing to act on.
            self._error(request_id, INTERNAL_ERROR, str(error) or type(error).__name__)
            return
        try:
            self._write({"jsonrpc": "2.0", "id": request_id, "result": result})
        except TransportClosed:
            pass  # the peer is gone; there is nobody to tell

    def _resolve(self, request_id: Any, frame: dict) -> None:
        future = self._pending.get(str(request_id))
        if future is None or future.done():
            return  # a late answer to something already timed out
        error = frame.get("error")
        if isinstance(error, dict):
            future.set_exception(
                JsonRpcError(
                    error.get("message") or "the peer returned an error",
                    error.get("code") if isinstance(error.get("code"), int) else None,
                    error.get("data"),
                )
            )
            return
        future.set_result(frame.get("result"))

    def _error(self, request_id: Any, code: int, message: str) -> None:
        try:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": code, "message": message},
                }
            )
        except TransportClosed:
            pass

    def _fail_pending(self, error: Exception) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(error)


def _parse(line: str) -> Optional[dict]:
    try:
        frame = json.loads(line)
    except json.JSONDecodeError:
        return None
    return frame if isinstance(frame, dict) else None


__all__ = [
    "JsonRpcTransport",
    "JsonRpcError",
    "TransportClosed",
    "MethodNotFound",
    "Reader",
    "Writer",
    "stdin_reader",
    "stdout_writer",
    "pipe",
    "duplex",
    "REQUEST_ID_PREFIX",
    "METHOD_NOT_FOUND",
    "INTERNAL_ERROR",
]
