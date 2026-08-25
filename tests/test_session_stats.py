"""Session stats — Requirement 4.

Measured over a conversation the **agent loop actually produced**, not a
hand-built log. That matters here more than usual: the reference's unit reads
live vocabulary objects, this port's log holds encoded payloads, and a
transcription would report zero for everything on a real conversation while
passing any test that fed it hand-made events.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from plugkit import Context, PointsService, ToolsService

from pydsh.agent import Agent, AgentOptions
from pydsh.llm import (
    ChunkType,
    GenerateOptions,
    LlmAdapter,
    LlmProviderInfo,
    LlmService,
    StreamChunk,
)
from pydsh.session import (
    SESSION_STATS_KEY,
    SessionProjections,
    SessionStats,
    SessionStore,
)

pytestmark = pytest.mark.asyncio


class Talker(LlmAdapter):
    """Answers in text, optionally asking for a tool first, with real delays."""

    def __init__(self, tool_call: tuple[str, str, str] | None = None, usage=None):
        self.tool_call = tool_call
        self.usage = usage
        self.attempts = 0

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        self.attempts += 1
        await asyncio.sleep(0.01)  # thinking time, before any token
        if self.tool_call and self.attempts == 1:
            call_id, name, arguments = self.tool_call
            yield StreamChunk(
                type=ChunkType.TOOL_CALL_DELTA,
                index=0,
                tool_call_id=call_id,
                tool_call_name=name,
                arguments_delta=arguments,
            )
            yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "tool-calls"})
            return
        yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text="hello")
        await asyncio.sleep(0.01)  # decode time, after the first token
        yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text=" there")
        if self.usage is not None:
            yield StreamChunk(type=ChunkType.USAGE, usage=self.usage)
        yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=provider)


def tool(name: str, body):
    class _Tool:
        pass

    t = _Tool()
    t.name = name
    t.description = ""
    t.parameters = {}
    t.execute = body
    return t


async def build(adapter: LlmAdapter, tools=()) -> tuple[Context, Agent]:
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(LlmService)
    await root.plugin(SessionProjections)
    await root.plugin(SessionStats)
    if tools:
        await root.plugin(PointsService)
        await root.plugin(ToolsService)
        for t in tools:
            root.tools.register(t)
    root.llm.register_adapter(["acme"], adapter)
    session = root.sessions.create()
    return root, Agent(root, session, AgentOptions(provider="acme", model="a-1"))


def stats(root: Context, agent: Agent) -> dict:
    return root.session_projections.snapshot(agent.session)["values"][
        SESSION_STATS_KEY
    ]


# --------------------------------------------------------------------------- #
# Counting (R4.1)
# --------------------------------------------------------------------------- #
async def test_a_fresh_session_is_all_zeros():
    root, agent = await build(Talker())
    assert stats(root, agent) == {
        "turns": 0,
        "steps": 0,
        "llm_ms": 0.0,
        "tool_ms": 0.0,
        "ttft_ms": 0.0,
        "ttft_steps": 0,
        "decode_ms": 0.0,
        "decode_tokens": 0,
    }


async def test_turns_and_steps_are_counted():
    root, agent = await build(Talker())
    await agent.run("one")
    await agent.run("two")
    measured = stats(root, agent)
    assert measured["turns"] == 2
    assert measured["steps"] == 2


async def test_a_multi_step_turn_counts_one_turn():
    """R4.1 — a turn is counted once however many steps it took."""

    async def echo(arguments, execution=None):
        return "done"

    root, agent = await build(
        Talker(tool_call=("c1", "echo", "{}")), tools=[tool("echo", echo)]
    )
    await agent.run("go")
    measured = stats(root, agent)
    assert measured["turns"] == 1
    assert measured["steps"] == 2  # the tool call, then the answer


# --------------------------------------------------------------------------- #
# Timings (R4.2–R4.5, R4.7)
# --------------------------------------------------------------------------- #
async def test_model_time_is_measured_on_a_real_conversation():
    """R4.7 — the assertion the reference's version would fail.

    Its unit reads a live `StreamChunk`; this log holds an encoded one. A
    transcription reports zero here, plausibly, and nobody notices.
    """
    root, agent = await build(Talker())
    await agent.run("go")
    measured = stats(root, agent)
    assert measured["llm_ms"] > 0


async def test_time_to_first_token_is_measured_and_counted():
    """R4.3"""
    root, agent = await build(Talker())
    await agent.run("go")
    measured = stats(root, agent)
    assert measured["ttft_steps"] == 1
    assert measured["ttft_ms"] > 0
    assert measured["ttft_ms"] <= measured["llm_ms"]


async def test_decode_is_measured_only_when_usage_was_reported():
    """R4.4 — tokens per second needs both halves, or neither is meaningful."""
    without = await build(Talker())
    await without[1].run("go")
    assert stats(*without)["decode_tokens"] == 0
    assert stats(*without)["decode_ms"] == 0

    with_usage = await build(Talker(usage={"output_tokens": 12}))
    await with_usage[1].run("go")
    measured = stats(*with_usage)
    assert measured["decode_tokens"] == 12
    assert measured["decode_ms"] > 0


async def test_tool_time_pairs_a_result_back_to_its_call():
    """R4.5 — pairing over the encoded tool-result message."""

    async def slow(arguments, execution=None):
        await asyncio.sleep(0.02)
        return "done"

    root, agent = await build(
        Talker(tool_call=("c1", "slow", "{}")), tools=[tool("slow", slow)]
    )
    await agent.run("go")
    measured = stats(root, agent)
    assert measured["tool_ms"] > 0


async def test_tool_time_is_zero_when_no_tool_ran():
    root, agent = await build(Talker())
    await agent.run("go")
    assert stats(root, agent)["tool_ms"] == 0


# --------------------------------------------------------------------------- #
# Bookkeeping does not leak or grow (R4.6)
# --------------------------------------------------------------------------- #
async def test_the_view_exposes_only_the_totals():
    """The open step and the pending calls are bookkeeping, not results."""
    root, agent = await build(Talker())
    await agent.run("go")
    assert "open_step" not in stats(root, agent)
    assert "pending_calls" not in stats(root, agent)


async def test_an_unpaired_call_is_dropped_at_the_turn_boundary():
    """R4.6 — a cancelled turn must not leave a call pending forever."""
    root, agent = await build(Talker())
    session = agent.session
    session.append("turn/start", {"turn": 1})
    session.append("tool/call", {
        "turn": 1, "step": 1, "callId": "orphan", "name": "x", "arguments": "{}"
    })
    session.append("turn/end", {"turn": 1, "reason": {"kind": "cancelled"}})

    rows = root.session_projections.checkpoint(session)
    assert rows[SESSION_STATS_KEY]["val"]["pending_calls"] == {}


async def test_stats_are_published_on_the_change_stream():
    seen = []
    root, agent = await build(Talker())
    root.session_projections.on_changed(
        lambda s, key, value, seq: seen.append((key, value["turns"]))
    )
    await agent.run("go")
    assert seen[-1][0] == SESSION_STATS_KEY
    assert seen[-1][1] == 1


async def test_the_service_offers_a_direct_read():
    root, agent = await build(Talker())
    await agent.run("go")
    assert root.session_stats.of(agent.session)["turns"] == 1
