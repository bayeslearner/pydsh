"""The JSON-RPC transport — Requirement 1, properties 1 and 2.

Two of these tests would fail against the reference, and both are the kind of
failure that only appears once someone leans on the connection: a handler that
stalls every frame behind it, and a cross-thread hand-off that is not
thread-safe.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

import pytest

from pydsh.runtime import (
    INTERNAL_ERROR,
    METHOD_NOT_FOUND,
    JsonRpcError,
    JsonRpcTransport,
    MethodNotFound,
    TransportClosed,
    duplex,
    pipe,
)

pytestmark = pytest.mark.asyncio


def connected() -> tuple[JsonRpcTransport, JsonRpcTransport]:
    """Two transports wired to each other, both reading."""
    (read_a, write_a), (read_b, write_b) = duplex()
    a = JsonRpcTransport(read_a, write_a)
    b = JsonRpcTransport(read_b, write_b)
    a.start()
    b.start()
    return a, b


def capturing() -> tuple[JsonRpcTransport, list]:
    """A transport whose writes land in a list."""
    written: list = []
    read, _ = pipe()
    return JsonRpcTransport(read, lambda line: written.append(json.loads(line))), written


# --------------------------------------------------------------------------- #
# R1.1–R1.4 — sending and matching
# --------------------------------------------------------------------------- #
async def test_a_request_gets_its_answer():
    """R1.2."""
    a, b = connected()
    b.on_request(lambda method, params: _echo(method, params))
    try:
        assert await a.request("ping", {"x": 1}) == {"method": "ping", "params": {"x": 1}}
    finally:
        await a.close()
        await b.close()


async def _echo(method: str, params: dict) -> dict:
    return {"method": method, "params": params}


async def test_a_request_can_time_out():
    a, b = connected()

    async def never(method, params):
        await asyncio.sleep(5)

    b.on_request(never)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await a.request("slow", timeout=0.05)
    finally:
        await a.close()
        await b.close()


async def test_an_error_frame_carries_its_code_and_data():
    """R1.3."""
    transport, _ = capturing()
    transport._pending["req_1"] = asyncio.get_running_loop().create_future()
    transport._resolve(
        "req_1",
        {"id": "req_1", "error": {"code": -1, "message": "no", "data": {"why": "x"}}},
    )
    with pytest.raises(JsonRpcError) as caught:
        await transport._pending["req_1"]
    assert caught.value.code == -1 and caught.value.data == {"why": "x"}


async def test_a_notification_carries_no_id():
    """R1.4."""
    transport, written = capturing()
    transport.notify("something.happened", {"x": 1})
    assert written == [
        {"jsonrpc": "2.0", "method": "something.happened", "params": {"x": 1}}
    ]
    assert "id" not in written[0]


async def test_a_notification_without_params_omits_the_member():
    transport, written = capturing()
    transport.notify("bare")
    assert written == [{"jsonrpc": "2.0", "method": "bare"}]


async def test_notifications_reach_the_other_end():
    a, b = connected()
    seen: list = []
    b.on_notification(lambda method, params: seen.append((method, params)))
    try:
        a.notify("hello", {"x": 1})
        await asyncio.sleep(0.01)
        assert seen == [("hello", {"x": 1})]
    finally:
        await a.close()
        await b.close()


# --------------------------------------------------------------------------- #
# R1.5 — concurrency (property 1)
# --------------------------------------------------------------------------- #
async def test_a_slow_handler_does_not_stall_the_reader():
    """Property 1 (R1.5, I1) — the reference awaits handlers in the read loop."""
    a, b = connected()

    async def handler(method, params):
        if method == "slow":
            await asyncio.sleep(0.2)
            return "slow done"
        return "fast done"

    b.on_request(handler)
    try:
        slow = asyncio.ensure_future(a.request("slow"))
        await asyncio.sleep(0.01)
        fast = await asyncio.wait_for(a.request("fast"), timeout=0.1)
        assert fast == "fast done"
        assert not slow.done(), "the slow one should still be running"
        assert await slow == "slow done"
    finally:
        await a.close()
        await b.close()


async def test_a_handler_can_call_back_into_the_peer():
    """I1 — awaited inline this deadlocks, because the answer arrives on the
    very loop that is waiting for the handler."""
    a, b = connected()

    a.on_request(lambda method, params: _echo(method, params))

    async def calls_back(method, params):
        answer = await b.request("what-do-you-say", {"n": 1})
        return {"asked_back": answer}

    b.on_request(calls_back)
    try:
        result = await asyncio.wait_for(a.request("go"), timeout=2)
        assert result["asked_back"]["method"] == "what-do-you-say"
    finally:
        await a.close()
        await b.close()


# --------------------------------------------------------------------------- #
# R1.6, R1.7 — answers and bad input
# --------------------------------------------------------------------------- #
async def test_an_unhandled_method_answers_method_not_found():
    """R1.6."""
    a, b = connected()
    try:
        with pytest.raises(JsonRpcError) as caught:
            await a.request("anything")
        assert caught.value.code == METHOD_NOT_FOUND
    finally:
        await a.close()
        await b.close()


async def test_a_handler_saying_not_mine_answers_method_not_found():
    a, b = connected()

    async def handler(method, params):
        raise MethodNotFound(f"no method {method!r} here")

    b.on_request(handler)
    try:
        with pytest.raises(JsonRpcError) as caught:
            await a.request("wat")
        assert caught.value.code == METHOD_NOT_FOUND
        assert "wat" in str(caught.value)
    finally:
        await a.close()
        await b.close()


async def test_a_handler_that_raises_answers_internal_error_with_its_message():
    """R1.6 — a client that cannot see why has nothing to act on."""
    a, b = connected()

    async def handler(method, params):
        raise ValueError("the disk is on fire")

    b.on_request(handler)
    try:
        with pytest.raises(JsonRpcError) as caught:
            await a.request("boom")
        assert caught.value.code == INTERNAL_ERROR
        assert "disk is on fire" in str(caught.value)
    finally:
        await a.close()
        await b.close()


@pytest.mark.parametrize("line", ["", "   ", "not json", "[1,2,3]", '"a string"'])
async def test_a_bad_line_is_skipped_not_fatal(line):
    """R1.7, I5 — one stray print must not kill a working connection."""
    read, write = pipe()
    transport = JsonRpcTransport(read, lambda text: None)
    transport.start()
    try:
        write(line)
        await asyncio.sleep(0.01)
        assert not transport.closed
    finally:
        await transport.close()


async def test_a_response_for_an_unknown_request_is_dropped():
    transport, _ = capturing()
    transport._resolve("req_nobody", {"id": "req_nobody", "result": 1})
    assert not transport._pending


# --------------------------------------------------------------------------- #
# R1.8 — closing (property 2)
# --------------------------------------------------------------------------- #
async def test_closing_fails_everything_pending():
    """Property 2 (R1.8, I4)."""
    a, b = connected()

    async def never(method, params):
        await asyncio.sleep(5)

    b.on_request(never)
    first = asyncio.ensure_future(a.request("one"))
    second = asyncio.ensure_future(a.request("two"))
    await asyncio.sleep(0.01)

    await a.close("the peer went away")
    for pending in (first, second):
        with pytest.raises(TransportClosed) as caught:
            await pending
        assert "went away" in str(caught.value)
    await b.close()


async def test_the_peer_closing_fails_pending_requests():
    a, b = connected()

    async def never(method, params):
        await asyncio.sleep(5)

    b.on_request(never)
    pending = asyncio.ensure_future(a.request("one"))
    await asyncio.sleep(0.01)

    # The reader sees end of input, which is what a closed pipe looks like.
    a._reader = _eof
    a._task.cancel()
    a._task = None
    a.start()
    with pytest.raises(TransportClosed):
        await asyncio.wait_for(pending, timeout=1)
    await a.close()
    await b.close()


async def _eof() -> Optional[str]:
    return None


async def test_close_is_idempotent():
    a, b = connected()
    await a.close()
    await a.close()
    assert a.closed
    await b.close()


async def test_a_closed_transport_refuses_requests():
    a, b = connected()
    await a.close()
    with pytest.raises(TransportClosed):
        await a.request("anything")
    await b.close()


async def test_a_write_failure_fails_pending_requests():
    def explode(line: str) -> None:
        raise OSError("the pipe is gone")

    read, _ = pipe()
    transport = JsonRpcTransport(read, explode)
    with pytest.raises(TransportClosed):
        await transport.request("anything")


async def test_a_notification_never_raises_at_the_emitter():
    """I3 — an observer cannot turn a committed fact into a failure."""

    def explode(line: str) -> None:
        raise OSError("the pipe is gone")

    read, _ = pipe()
    transport = JsonRpcTransport(read, explode)
    transport.notify("session.event", {"x": 1})  # must not raise


# --------------------------------------------------------------------------- #
# R1.9, R1.10 — the default reader and writer
# --------------------------------------------------------------------------- #
async def test_the_stdin_reader_hands_over_through_the_loop(monkeypatch):
    """I2 — `asyncio.Queue` is not thread-safe, and the reference ignores that."""
    import io

    from pydsh.runtime import protocol

    handed: list = []
    loop = asyncio.get_running_loop()
    original = loop.call_soon_threadsafe

    def record(callback, *args):
        handed.append(args)
        return original(callback, *args)

    monkeypatch.setattr(loop, "call_soon_threadsafe", record)
    monkeypatch.setattr(protocol.sys, "stdin", io.StringIO("one\ntwo\n"))

    read = protocol.stdin_reader()
    assert await read() == "one\n"
    assert await read() == "two\n"
    assert await read() is None  # end of input
    assert handed, "lines were queued without going through the loop"


async def test_the_stdout_writer_writes_a_line_and_flushes(monkeypatch):
    """R1.10."""
    import io

    from pydsh.runtime import protocol

    buffer = io.StringIO()
    monkeypatch.setattr(protocol.sys, "stdout", buffer)
    protocol.stdout_writer()('{"jsonrpc":"2.0"}')
    assert buffer.getvalue() == '{"jsonrpc":"2.0"}\n'


async def test_a_pipe_carries_what_is_written_to_it():
    read, write = pipe()
    write("hello")
    assert await read() == "hello"
