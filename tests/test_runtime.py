"""The runtime server and client — Requirements 2 and 3, property 3.

Most of these run both ends in one process over an in-memory duplex, which is
the honest way to test protocol behaviour. One spawns a **real child process**
running `python -m pydsh.runtime`, because a runtime nobody can actually start
is not a runtime.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, AsyncIterator

import pytest

from plugkit import Context, PointsService

from pydsh import (
    AgentOptions,
    ChunkType,
    GenerateOptions,
    LlmAdapter,
    LlmProviderInfo,
    LlmService,
    SessionStore,
    StreamChunk,
    core_profile,
)
from pydsh.boot import Harness, mount_profile, resolve_profile, unmount
from pydsh.runtime import (
    EVENT_NOTIFICATION,
    METHODS,
    PROTOCOL_VERSION,
    SERVER_NAME,
    STATUS_NOTIFICATION,
    JsonRpcError,
    JsonRpcTransport,
    RuntimeClient,
    RuntimeServer,
    TransportClosed,
    blocks_from_wire,
    duplex,
    pipe,
)
from pydsh.runtime.protocol import METHOD_NOT_FOUND

pytestmark = pytest.mark.asyncio


class Answerer(LlmAdapter):
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies) or ["the answer"]
        self.calls = 0

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text=reply)
        yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=provider)


class Wired:
    """A server and a client transport, wired together in one process."""

    def __init__(self, harness, server, client, mounted) -> None:
        self.harness = harness
        self.server = server
        self.client = client
        self._mounted = mounted

    async def close(self) -> None:
        await self.client.close()
        await self.server.transport.close()
        await self.harness.close()


async def wired(adapter=None, provider: str = "acme") -> Wired:
    harness = Harness(env={}, options=AgentOptions(provider="acme", model="a-1"))
    ctx = await harness.start()
    ctx.llm.register_adapter(["acme"], adapter or Answerer())

    (read_s, write_s), (read_c, write_c) = duplex()
    server_transport = JsonRpcTransport(read_s, write_s)
    server = RuntimeServer(ctx, server_transport)
    server_transport.start()

    client = RuntimeClient(
        provider=provider, model="a-1", transport=JsonRpcTransport(read_c, write_c)
    )
    await client.start()
    return Wired(harness, server, client, None)


# --------------------------------------------------------------------------- #
# R2.2, R2.3 — the handshake
# --------------------------------------------------------------------------- #
async def test_initialize_reports_who_and_what():
    """R2.2."""
    w = await wired()
    try:
        info = w.client.server_info
        assert info["server"]["name"] == SERVER_NAME
        assert info["protocol_version"] == PROTOCOL_VERSION
        assert info["providers"] == ["acme"]
        assert set(info["methods"]) == set(METHODS)
    finally:
        await w.close()


async def test_the_handshake_fixes_the_route():
    w = await wired()
    try:
        assert w.server.route["provider"] == "acme"
        assert w.server.route["model"] == "a-1"
    finally:
        await w.close()


async def test_an_unroutable_provider_is_refused_naming_what_is_routable():
    """R2.3 — the reference silently mounts a vendor's adapter here instead."""
    with pytest.raises(JsonRpcError) as caught:
        await wired(provider="nowhere")
    assert "nowhere" in str(caught.value)
    assert "acme" in str(caught.value)


async def test_no_provider_named_is_accepted():
    w = await wired(provider="")
    try:
        assert w.client.server_info["providers"] == ["acme"]
    finally:
        await w.close()


# --------------------------------------------------------------------------- #
# R2.4, R2.5 — prompting and running
# --------------------------------------------------------------------------- #
async def test_run_delivers_and_waits():
    """R2.5, R3.3."""
    w = await wired(Answerer("forty-two"))
    try:
        result = await w.client.session("chat-1").run("what is it?")
        assert result.session_id == "chat-1"
        assert result.final_response == "forty-two"
        assert result.event_count > 0
    finally:
        await w.close()


async def test_prompt_returns_without_waiting():
    """R2.4."""
    w = await wired()
    try:
        answer = await w.client.session("chat-1").send("go")
        assert answer["session_id"] == "chat-1" and answer["message_id"]
    finally:
        await w.close()


async def test_content_blocks_arrive_intact():
    w = await wired()
    try:
        result = await w.client.session("chat-1").run(
            [{"type": "text", "text": "structured"}]
        )
        assert result.final_response == "the answer"
    finally:
        await w.close()


async def test_a_prompt_with_no_session_id_is_refused():
    w = await wired()
    try:
        with pytest.raises(JsonRpcError) as caught:
            await w.client.request("session/run", {"content": "hi"})
        assert "session_id" in str(caught.value)
    finally:
        await w.close()


async def test_a_prompt_with_no_content_is_refused():
    w = await wired()
    try:
        with pytest.raises(JsonRpcError) as caught:
            await w.client.request("session/run", {"session_id": "c", "content": []})
        assert "content" in str(caught.value)
    finally:
        await w.close()


async def test_sessions_can_be_listed():
    w = await wired()
    try:
        await w.client.session("chat-1").run("hello")
        answer = await w.client.request("session/list")
        assert "chat-1" in answer["sessions"]
    finally:
        await w.close()


async def test_a_second_run_continues_the_same_session():
    w = await wired(Answerer("first", "second"))
    try:
        session = w.client.session("chat-1")
        await session.run("one")
        result = await session.run("two")
        assert result.final_response == "second"
    finally:
        await w.close()


# --------------------------------------------------------------------------- #
# R2.6, R2.7 — forwarding (property 3)
# --------------------------------------------------------------------------- #
async def test_session_events_are_forwarded_as_they_happen():
    """R2.6, R3.4."""
    w = await wired()
    seen: list = []
    release = w.client.on_event(seen.append)
    try:
        await w.client.session("chat-1").run("hello")
        types = [event["type"] for event in seen]
        assert "turn/start" in types and "assistant/message" in types
        assert all(event["session_id"] == "chat-1" for event in seen)
    finally:
        release()
        await w.close()


async def test_agent_status_is_forwarded():
    w = await wired()
    seen: list = []
    release = w.client.on_status(seen.append)
    try:
        await w.client.session("chat-1").run("hello")
        assert {s["status"] for s in seen} >= {"running", "idle"}
    finally:
        release()
        await w.close()


async def test_a_runs_result_carries_the_events_it_saw():
    w = await wired()
    try:
        result = await w.client.session("chat-1").run("hello")
        assert result.events and result.events[0]["session_id"] == "chat-1"
    finally:
        await w.close()


async def test_events_for_another_session_are_not_mixed_in():
    w = await wired()
    try:
        await w.client.session("chat-a").run("hello")
        result = await w.client.session("chat-b").run("hello")
        assert all(e["session_id"] == "chat-b" for e in result.events)
    finally:
        await w.close()


async def test_forwarding_never_raises_into_the_append():
    """Property 3 (R2.7, I3) — an observer cannot undo a committed append."""
    harness = Harness(env={}, options=AgentOptions(provider="acme", model="a-1"))
    ctx = await harness.start()
    ctx.llm.register_adapter(["acme"], Answerer())

    def explode(line: str) -> None:
        raise OSError("the client's pipe is gone")

    read, _ = pipe()
    transport = JsonRpcTransport(read, explode)
    RuntimeServer(ctx, transport)
    try:
        session = ctx.sessions.create("chat-1")
        # The append must succeed even though forwarding it cannot.
        event = session.append("turn/start", {"turn": 1})
        assert event.seq == 1
        assert session.events[-1].type == "turn/start"
    finally:
        await transport.close()
        await harness.close()


# --------------------------------------------------------------------------- #
# R2.8, R2.9 — shutdown and unknown methods
# --------------------------------------------------------------------------- #
async def test_shutdown_is_idempotent():
    """R2.8."""
    w = await wired()
    try:
        assert (await w.client.request("shutdown")) == {"ok": True}
        assert (await w.client.request("shutdown"))["already"] is True
    finally:
        await w.close()


async def test_a_prompt_after_shutdown_is_refused():
    w = await wired()
    try:
        await w.client.request("shutdown")
        with pytest.raises(JsonRpcError) as caught:
            await w.client.session("chat-1").run("hello")
        assert "shut down" in str(caught.value)
    finally:
        await w.close()


async def test_an_unknown_method_answers_rather_than_crashing():
    """R2.9."""
    w = await wired()
    try:
        with pytest.raises(JsonRpcError) as caught:
            await w.client.request("session/teleport")
        assert caught.value.code == METHOD_NOT_FOUND
        assert "session/run" in str(caught.value)
        # And the connection still works.
        assert (await w.client.request("session/list"))["sessions"] == []
    finally:
        await w.close()


# --------------------------------------------------------------------------- #
# blocks_from_wire
# --------------------------------------------------------------------------- #
async def test_a_bare_string_becomes_one_text_block():
    blocks = blocks_from_wire("hello")
    assert len(blocks) == 1 and blocks[0].text == "hello"


async def test_text_and_tool_call_blocks_are_translated():
    blocks = blocks_from_wire(
        [
            {"type": "text", "text": "look"},
            {"type": "tool-call", "id": "c1", "name": "bash", "arguments": "{}"},
        ]
    )
    assert blocks[0].text == "look"
    assert blocks[1].id == "c1" and blocks[1].name == "bash"


async def test_an_unknown_block_is_passed_through_not_dropped():
    """Losing part of a message is the one thing a translation must not do."""
    blocks = blocks_from_wire([{"type": "hologram", "payload": 1}])
    assert blocks == [{"type": "hologram", "payload": 1}]


@pytest.mark.parametrize("bad", [7, {"type": "text"}, [1, 2]])
async def test_malformed_content_is_refused(bad):
    with pytest.raises(TypeError):
        blocks_from_wire(bad)


# --------------------------------------------------------------------------- #
# R3 — the client
# --------------------------------------------------------------------------- #
async def test_an_unnamed_session_gets_a_name():
    """R3.2."""
    w = await wired()
    try:
        first = w.client.session()
        assert first.id.startswith("session-")
        assert first.id != w.client.session().id
    finally:
        await w.close()


async def test_close_is_idempotent():
    """R3.5."""
    w = await wired()
    await w.client.close()
    await w.client.close()
    await w.close()


async def test_a_closed_client_refuses_work():
    w = await wired()
    await w.client.close()
    with pytest.raises(TransportClosed):
        await w.client.request("session/list")
    await w.close()


# --------------------------------------------------------------------------- #
# A real child process
# --------------------------------------------------------------------------- #
PROFILE_MODULE = '''
from plugkit import PointsService, ToolsService
from pydsh import (AgentLoop, AgentRegistry, LlmService, SessionStore, TokenMeter,
                   ChunkType, StreamChunk, LlmAdapter, LlmProviderInfo)


class Fixed(LlmAdapter):
    async def stream(self, options):
        yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text="from the child")
        yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})

    def provider_info(self, provider):
        return LlmProviderInfo(id=provider, name=provider)


class FixedAdapter(LlmService):
    """Not a service — a plugin that registers the fixed adapter."""


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


async def test_a_real_child_runtime_answers(tmp_path):
    """R3.1, R3.6 — a runtime nobody can start is not a runtime."""
    profile = tmp_path / "child_profile.py"
    profile.write_text(PROFILE_MODULE)

    client = RuntimeClient(
        provider="acme",
        model="a-1",
        command=[
            sys.executable, "-u", "-m", "pydsh.runtime",
            "--profile", str(profile),
            "--home", str(tmp_path / "home"),
        ],
    )
    async with client:
        assert client.server_info["server"]["name"] == SERVER_NAME
        result = await client.session("chat-1").run("hello", timeout=30)
        assert result.final_response == "from the child"
        assert result.events, "no events were streamed back"


async def test_a_dead_runtime_fails_the_request_rather_than_hanging(tmp_path):
    """R3.6 — a dead runtime will never answer; say so instead of waiting."""
    client = RuntimeClient(
        provider="",
        command=[sys.executable, "-c", "import sys; sys.exit(0)"],
        handshake_timeout=5,
    )
    with pytest.raises(Exception):
        await client.start()
    await client.close()
