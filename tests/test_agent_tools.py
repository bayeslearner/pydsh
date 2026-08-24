"""Tool execution through the kernel pipeline — Requirement 5 and property 2.

The loop does not own a tool registry; plugkit does. What this proves is that
the loop hands calls to that pipeline correctly, keeps the log deterministic
regardless of which call finishes first, and turns every possible failure into
something the model can read rather than something that aborts the turn (I3).
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
from pydsh.message import decode_payload
from pydsh.session import SessionStore

pytestmark = pytest.mark.asyncio


class TwoTurnAdapter(LlmAdapter):
    """Asks for tool calls once, then answers plainly."""

    def __init__(self, calls: list[tuple[str, str, str]]) -> None:
        self.calls = calls
        self.attempts = 0
        self.requests: list[GenerateOptions] = []

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        self.attempts += 1
        self.requests.append(options)
        if self.attempts == 1:
            for index, (call_id, name, arguments) in enumerate(self.calls):
                yield StreamChunk(
                    type=ChunkType.TOOL_CALL_DELTA,
                    index=index,
                    tool_call_id=call_id,
                    tool_call_name=name,
                    arguments_delta=arguments,
                )
            yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "tool-calls"})
            return
        yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text="done")
        yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=provider)


def tool(name: str, body, parameters: dict | None = None):
    """A minimal tool object — plugkit accepts anything with these attributes."""

    class _Tool:
        pass

    t = _Tool()
    t.name = name
    t.description = f"the {name} tool"
    t.parameters = parameters or {}
    t.execute = body
    return t


async def build(calls, tools=(), **option_kwargs) -> tuple[Context, Agent, TwoTurnAdapter]:
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(LlmService)
    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    adapter = TwoTurnAdapter(list(calls))
    root.llm.register_adapter(["acme"], adapter)
    for t in tools:
        root.tools.register(t)
    session = root.sessions.create()
    options = AgentOptions(provider="acme", model="a-1", **option_kwargs)
    return root, Agent(root, session, options), adapter


def tool_events(agent: Agent, kind: str) -> list:
    return [e.data for e in agent.session.events if e.type == kind]


def result_text(data) -> str:
    message = decode_payload(data["message"])
    return message.content[0].content[0].text


# --------------------------------------------------------------------------- #
# The pipeline is reached correctly
# --------------------------------------------------------------------------- #
async def test_tool_schemas_reach_the_model():
    """R5.1 — the model is told what it may call, in the shape it expects."""

    async def body(arguments, execution=None):
        return "ok"

    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    root, agent, adapter = await build(
        [("c1", "echo", "{}")], tools=[tool("echo", body, schema)]
    )
    await agent.run("go")

    assert adapter.requests[0].tools == [
        {"name": "echo", "description": "the echo tool", "parameters": schema}
    ]


async def test_no_tools_means_no_tool_list_rather_than_an_empty_one():
    """R5.1 — an empty list and "none registered" read the same to a provider."""
    root, agent, adapter = await build([], tools=[])
    await agent.run("go")
    assert adapter.requests[0].tools is None


async def test_arguments_are_parsed_into_the_pipeline():
    """R5.2 — the model's text becomes the dict a tool actually receives."""
    received: list[dict] = []

    async def body(arguments, execution=None):
        received.append(arguments)
        return "ok"

    root, agent, _ = await build(
        [("c1", "echo", '{"path": "a.txt", "n": 3}')], tools=[tool("echo", body)]
    )
    await agent.run("go")
    assert received == [{"path": "a.txt", "n": 3}]


async def test_the_result_reaches_the_next_step_as_history():
    """R3.5 — the tool's output is what the second model call sees."""

    async def body(arguments, execution=None):
        return "the answer is 42"

    root, agent, _ = await build([("c1", "ask", "{}")], tools=[tool("ask", body)])
    await agent.run("go")

    results = tool_events(agent, "tool/result")
    assert len(results) == 1
    assert result_text(results[0]) == "the answer is 42"
    assert results[0]["error"] is False


# --------------------------------------------------------------------------- #
# Property 2 — order is the model's order, not the clock's
# --------------------------------------------------------------------------- #
async def test_results_are_logged_in_call_order_under_inverted_latency():
    """R5.5 (I4) — the slow first call still lands first in the log."""

    async def slow(arguments, execution=None):
        await asyncio.sleep(0.02)
        return "slow"

    async def fast(arguments, execution=None):
        return "fast"

    root, agent, _ = await build(
        [("c1", "slow", "{}"), ("c2", "fast", "{}")],
        tools=[tool("slow", slow), tool("fast", fast)],
        max_parallel_tool_calls=2,
    )
    await agent.run("go")

    assert [d["name"] for d in tool_events(agent, "tool/call")] == ["slow", "fast"]
    assert [result_text(d) for d in tool_events(agent, "tool/result")] == ["slow", "fast"]


async def test_parallelism_is_bounded():
    """R5.4 — the bound is real, not decorative."""
    live = 0
    peak = 0

    async def body(arguments, execution=None):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        return "ok"

    calls = [(f"c{i}", "work", "{}") for i in range(4)]
    root, agent, _ = await build(
        calls, tools=[tool("work", body)], max_parallel_tool_calls=2
    )
    await agent.run("go")
    assert peak == 2


async def test_serial_is_the_default():
    """NF3 — the safe default, because tools share a working directory."""
    live = 0
    peak = 0

    async def body(arguments, execution=None):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.005)
        live -= 1
        return "ok"

    calls = [(f"c{i}", "work", "{}") for i in range(3)]
    root, agent, _ = await build(calls, tools=[tool("work", body)])
    await agent.run("go")
    assert peak == 1


# --------------------------------------------------------------------------- #
# I3 — every failure is a result, never an exception
# --------------------------------------------------------------------------- #
async def test_malformed_arguments_never_reach_the_tool():
    """R5.3 — a common real-model failure, answered rather than raised."""
    called = False

    async def body(arguments, execution=None):
        nonlocal called
        called = True
        return "ok"

    root, agent, _ = await build(
        [("c1", "echo", "{not json")], tools=[tool("echo", body)]
    )
    await agent.run("go")

    results = tool_events(agent, "tool/result")
    assert called is False
    assert results[0]["error"] is True
    assert "not valid JSON" in result_text(results[0])


async def test_non_object_arguments_are_rejected():
    """R5.3 — valid JSON is not enough; the pipeline needs an object."""
    root, agent, _ = await build([("c1", "echo", "[1, 2]")], tools=[])
    await agent.run("go")
    results = tool_events(agent, "tool/result")
    assert results[0]["error"] is True
    assert "must be a JSON object" in result_text(results[0])


async def test_unknown_tool_is_an_error_result():
    """R5.6 — the model asked for something that does not exist; tell it so."""
    root, agent, _ = await build([("c1", "nope", "{}")], tools=[])
    await agent.run("go")
    results = tool_events(agent, "tool/result")
    assert results[0]["error"] is True
    assert "UNKNOWN_TOOL" in result_text(results[0])


async def test_a_raising_tool_does_not_abort_the_turn():
    """R5.7 (I3) — the turn completes, with the failure in the history."""

    async def explode(arguments, execution=None):
        raise RuntimeError("the disk is on fire")

    root, agent, _ = await build([("c1", "boom", "{}")], tools=[tool("boom", explode)])
    await agent.run("go")

    results = tool_events(agent, "tool/result")
    assert results[0]["error"] is True
    ends = [e.data["reason"] for e in agent.session.events if e.type == "turn/end"]
    assert ends == [{"kind": "completed"}]


async def test_a_guard_denial_is_an_error_result():
    """R5.6 — a guard the loop knows nothing about still reaches the model."""

    async def body(arguments, execution=None):
        return "ok"

    root, agent, _ = await build([("c1", "rm", "{}")], tools=[tool("rm", body)])
    root.tools.guard(lambda execution: "not allowed here")
    await agent.run("go")

    results = tool_events(agent, "tool/result")
    assert results[0]["error"] is True
    assert "not allowed here" in result_text(results[0])


async def test_no_tools_service_means_no_schemas_and_a_clear_result():
    """A loop on a context with no tools must not pretend it has any."""
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(LlmService)
    adapter = TwoTurnAdapter([("c1", "echo", "{}")])
    root.llm.register_adapter(["acme"], adapter)
    session = root.sessions.create()
    agent = Agent(root, session, AgentOptions(provider="acme", model="a-1"))

    await agent.run("go")
    results = tool_events(agent, "tool/result")
    assert results[0]["error"] is True
    assert "no tools are available" in result_text(results[0])
