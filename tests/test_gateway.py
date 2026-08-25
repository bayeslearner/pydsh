"""The gateway — Requirements 1 and 2, properties 1 and 2.

A fake connection stands in for a socket, which is exactly the point of the
adapter: the gateway's own logic has nothing to do with WebSockets, and testing
it through one would only prove the library works.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Optional

import pytest

from pydsh import (
    AgentOptions,
    ChunkType,
    GenerateOptions,
    LlmAdapter,
    LlmProviderInfo,
    StreamChunk,
)
from pydsh.boot import Harness
from pydsh.gateway import (
    DEFAULT_HOST,
    DEFAULT_MAX_CONNECTIONS,
    MAX_FRAME_BYTES,
    REFUSED_REASON,
    FrameTooLarge,
    Gateway,
    connection_io,
)
from pydsh.runtime import EVENT_NOTIFICATION, JsonRpcTransport, RuntimeClient

pytestmark = pytest.mark.asyncio


class Answerer(LlmAdapter):
    def __init__(self, reply: str = "the answer") -> None:
        self.reply = reply

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text=self.reply)
        yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=provider)


class FakeConnection:
    """A socket's shape: `recv`, `send`, `close`. No socket."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list = []
        self.closed = False
        self._peer: Optional["FakeConnection"] = None

    async def recv(self) -> Optional[str]:
        frame = await self.inbox.get()
        if frame is None:
            raise ConnectionError("closed")
        return frame

    async def send(self, frame: str) -> None:
        if self.closed:
            raise ConnectionError("closed")
        self.sent.append(frame)
        if self._peer is not None:
            self._peer.inbox.put_nowait(frame)

    async def close(self) -> None:
        self.closed = True
        self.inbox.put_nowait(None)

    def deliver(self, frame: str) -> None:
        self.inbox.put_nowait(frame)


def linked() -> tuple[FakeConnection, JsonRpcTransport]:
    """A connection the gateway serves, and a client transport on the far end."""
    server_side = FakeConnection()
    client_side = FakeConnection()
    server_side._peer = client_side
    client_side._peer = server_side

    read, write = connection_io(client_side)
    return server_side, JsonRpcTransport(read, write)


async def booted(reply: str = "the answer"):
    harness = Harness(env={}, options=AgentOptions(provider="acme", model="a-1"))
    ctx = await harness.start()
    ctx.llm.register_adapter(["acme"], Answerer(reply))
    return harness, ctx


# --------------------------------------------------------------------------- #
# R1 — the adapter
# --------------------------------------------------------------------------- #
async def test_recv_becomes_a_reader():
    """R1.1."""
    connection = FakeConnection()
    read, _ = connection_io(connection)
    connection.deliver("a frame")
    assert await read() == "a frame"


async def test_a_closed_connection_reads_as_end_of_input():
    """R1.2."""
    connection = FakeConnection()
    read, _ = connection_io(connection)
    await connection.close()
    assert await read() is None


async def test_bytes_are_decoded():
    connection = FakeConnection()
    read, _ = connection_io(connection)
    connection.deliver(b"binary frame")
    assert await read() == "binary frame"


async def test_an_oversized_frame_is_refused_and_closes_the_connection():
    """R1.3, I3 — a client sending one will send another."""
    connection = FakeConnection()
    read, _ = connection_io(connection, max_frame_bytes=16)
    connection.deliver("x" * 100)

    with pytest.raises(FrameTooLarge):
        await read()
    await asyncio.sleep(0)
    assert connection.closed


async def test_writing_does_not_block_and_does_not_raise():
    """R1.4 — the writer is called from `notify`, where raising is forbidden."""
    connection = FakeConnection()
    _, write = connection_io(connection)
    write("a frame")
    await asyncio.sleep(0)
    assert connection.sent == ["a frame"]

    await connection.close()
    write("another")  # must not raise
    await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# R2 — the gateway
# --------------------------------------------------------------------------- #
async def test_a_client_can_talk_to_the_gateway():
    """R2.1."""
    harness, ctx = await booted("hello from the gateway")
    gateway = Gateway(ctx, AgentOptions(provider="acme", model="a-1"))
    server_side, client_transport = linked()
    serving = asyncio.ensure_future(gateway.handle(server_side))
    client = RuntimeClient(provider="acme", model="a-1", transport=client_transport)
    try:
        await client.start()
        result = await client.session("chat-1").run("hello")
        assert result.final_response == "hello from the gateway"
    finally:
        await client.close()
        await gateway.close()
        serving.cancel()
        await harness.close()


async def test_two_clients_never_see_each_others_events():
    """Property 1 (R2.1, I1) — one shared server would send everyone everything."""
    harness, ctx = await booted()
    gateway = Gateway(ctx, AgentOptions(provider="acme", model="a-1"))

    a_side, a_transport = linked()
    b_side, b_transport = linked()
    serving = [
        asyncio.ensure_future(gateway.handle(a_side)),
        asyncio.ensure_future(gateway.handle(b_side)),
    ]
    a = RuntimeClient(provider="acme", model="a-1", transport=a_transport)
    b = RuntimeClient(provider="acme", model="a-1", transport=b_transport)

    a_events: list = []
    b_events: list = []
    try:
        await a.start()
        await b.start()
        a.on_event(a_events.append)
        b.on_event(b_events.append)

        await a.session("chat-a").run("hello")
        await asyncio.sleep(0.05)

        assert a_events, "the client that asked saw nothing"
        assert all(e["session_id"] == "chat-a" for e in a_events)
        assert b_events == [], "the other client saw a conversation that was not its own"
    finally:
        await a.close()
        await b.close()
        await gateway.close()
        for task in serving:
            task.cancel()
        await harness.close()


async def test_a_disconnect_releases_everything():
    """Property 2 (R2.2, I2)."""
    harness, ctx = await booted()
    gateway = Gateway(ctx, AgentOptions(provider="acme", model="a-1"))
    server_side, client_transport = linked()
    serving = asyncio.ensure_future(gateway.handle(server_side))
    await asyncio.sleep(0.05)
    assert gateway.connection_count == 1

    await server_side.close()
    await asyncio.wait_for(serving, timeout=2)
    assert gateway.connection_count == 0

    await gateway.close()
    await client_transport.close()
    await harness.close()


async def test_the_connection_limit_refuses_with_a_reason():
    """R2.3, I4 — a silent close is unattributable."""
    harness, ctx = await booted()
    gateway = Gateway(ctx, max_connections=1)

    first, _ = linked()
    serving = asyncio.ensure_future(gateway.handle(first))
    await asyncio.sleep(0.05)

    second = FakeConnection()
    await gateway.handle(second)

    assert gateway.connection_count == 1
    assert second.closed
    assert any(REFUSED_REASON in frame for frame in second.sent)

    await gateway.close()
    serving.cancel()
    await harness.close()


async def test_a_closed_gateway_refuses_new_connections():
    """R2.5."""
    harness, ctx = await booted()
    gateway = Gateway(ctx)
    await gateway.close()

    connection = FakeConnection()
    await gateway.handle(connection)
    assert gateway.connection_count == 0 and connection.closed
    await harness.close()


async def test_close_is_idempotent():
    harness, ctx = await booted()
    gateway = Gateway(ctx)
    await gateway.close()
    await gateway.close()
    await harness.close()


async def test_closing_the_gateway_releases_live_connections():
    harness, ctx = await booted()
    gateway = Gateway(ctx)
    server_side, client_transport = linked()
    serving = asyncio.ensure_future(gateway.handle(server_side))
    await asyncio.sleep(0.05)
    assert gateway.connection_count == 1

    await gateway.close()
    assert gateway.connection_count == 0
    serving.cancel()
    await client_transport.close()
    await harness.close()


async def test_the_defaults_are_loopback_and_bounded():
    """The gateway authenticates nobody, so it must not bind everything."""
    assert DEFAULT_HOST == "127.0.0.1"
    assert DEFAULT_MAX_CONNECTIONS > 0
    assert MAX_FRAME_BYTES > 0


async def test_serve_says_what_to_install_when_the_extra_is_missing(monkeypatch):
    """R2.4."""
    import builtins

    from pydsh.gateway import serve

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "websockets":
            raise ImportError("no websockets here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    harness, ctx = await booted()
    try:
        with pytest.raises(RuntimeError) as caught:
            await serve(ctx)
        assert "pydsh[ws]" in str(caught.value)
    finally:
        await harness.close()
