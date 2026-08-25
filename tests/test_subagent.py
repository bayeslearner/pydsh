"""The subagent tool — Requirement 2, property 2.

Every test here runs a real child agent through the real loop over a fake
adapter. The two that matter most are the ones the reference gets wrong:
parallel siblings must not exhaust the depth budget, and cancelling the parent
must actually stop the child.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from plugkit import Context, PointsService, ToolsService

from pydsh import (
    AgentLoop,
    AgentOptions,
    AgentRegistry,
    ChunkType,
    GenerateOptions,
    LlmAdapter,
    LlmProviderInfo,
    LlmService,
    SessionStore,
    StreamChunk,
    SubagentTool,
)
from pydsh.agent.subagent import DEPTH_ATTR, branch_depth

pytestmark = pytest.mark.asyncio


class Answerer(LlmAdapter):
    def __init__(self, reply: str = "the child's answer") -> None:
        self.reply = reply
        self.calls = 0

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        self.calls += 1
        yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text=self.reply)
        yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=provider)


class SlowAnswerer(LlmAdapter):
    """Takes long enough that a cancellation lands mid-stream.

    It honours ``options.signal`` the way a real adapter does — which is the
    point: the signal it is handed is the child's, fused to the parent's turn,
    so this is what proves the fusion reaches all the way down to the socket.
    """

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finished = False
        self.signal = None

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        self.signal = options.signal
        self.started.set()
        yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text="thinking")
        for _ in range(200):
            if getattr(options.signal, "aborted", False):
                return
            await asyncio.sleep(0.01)
        self.finished = True
        yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=provider)


async def build(adapter=None, config=None):
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(LlmService)
    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    await root.plugin(AgentRegistry)
    await root.plugin(AgentLoop)
    settings = {"provider": "acme", "model": "a-1"}
    settings.update(config or {})
    await root.plugin(SubagentTool, settings)
    adapter = adapter or Answerer()
    root.llm.register_adapter(["acme"], adapter)
    return root, adapter


def parent_of(root, name: str = "chat-1"):
    session = root.sessions.create(name)
    return root.agent_loop.create_agent(
        session, AgentOptions(provider="acme", model="a-1")
    )


async def call(root, agent, prompt: str = "do the thing"):
    return await root.tools.execute("subagent", {"prompt": prompt}, caller=agent)


# --------------------------------------------------------------------------- #
# R2.1 — the child answers
# --------------------------------------------------------------------------- #
async def test_a_subagent_answers_the_standalone_prompt():
    root, adapter = await build()
    result = await call(root, parent_of(root))
    assert result.ok and "the child's answer" in str(result.value)
    assert adapter.calls == 1


async def test_the_child_sees_none_of_the_parent_conversation():
    """The point of a standalone prompt: the child has its own session."""
    seen: list = []

    class Watcher(Answerer):
        async def stream(self, options):
            seen.append(list(options.messages))
            async for chunk in super().stream(options):
                yield chunk

    root, _ = await build(Watcher())
    parent = parent_of(root)
    parent.session.append("turn/start", {"turn": 1})
    await call(root, parent, "count to three")

    assert len(seen) == 1
    texts = [
        b.text
        for message in seen[0]
        for b in message.content
        if getattr(b, "text", None)
    ]
    assert texts == ["count to three"]


async def test_an_empty_prompt_is_refused():
    root, adapter = await build()
    result = await root.tools.execute("subagent", {"prompt": "  "}, caller=parent_of(root))
    assert "self-contained prompt" in str(result.value)
    assert adapter.calls == 0


# --------------------------------------------------------------------------- #
# R2.2, R2.3 — depth (I3), property 2
# --------------------------------------------------------------------------- #
async def test_parallel_siblings_are_all_at_the_same_depth():
    """Property 2 (R2.2, I3) — the reference refuses the fifth of five.

    A shared counter measures how many subagents are *running*, so five
    started from one turn look like five levels of nesting and the last is
    refused for a depth that never existed.
    """
    root, adapter = await build(config={"max_depth": 2})
    parent = parent_of(root)

    results = await asyncio.gather(*(call(root, parent) for _ in range(5)))
    assert all(r.ok for r in results)
    assert all("exceeds the limit" not in str(r.value) for r in results)
    assert adapter.calls == 5


async def test_nesting_past_the_limit_is_refused():
    root, _ = await build(config={"max_depth": 2})
    parent = parent_of(root)
    setattr(parent, DEPTH_ATTR, 2)  # already two deep

    result = await call(root, parent)
    assert "exceeds the limit of 2" in str(result.value)


async def test_a_depth_of_zero_forbids_spawning():
    root, adapter = await build(config={"max_depth": 0})
    result = await call(root, parent_of(root))
    assert "exceeds the limit of 0" in str(result.value)
    assert adapter.calls == 0


async def test_a_child_records_its_own_depth():
    """So a grandchild counts from the child, not from the root."""
    depths: list = []

    class Recorder(Answerer):
        async def stream(self, options):
            depths.append(
                [branch_depth(a) for a in options_agents(root)]
            )
            async for chunk in super().stream(options):
                yield chunk

    def options_agents(root):
        return list(root.agent_loop._agents.values())

    root, _ = await build(Recorder())
    await call(root, parent_of(root))
    assert 1 in depths[0], "the child was not stamped with its branch depth"


# --------------------------------------------------------------------------- #
# R2.4 — cancellation (I4)
# --------------------------------------------------------------------------- #
class Delegator(LlmAdapter):
    """A parent that calls `subagent` on its first step, then answers."""

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        self.calls += 1
        if self.calls == 1:
            yield StreamChunk(
                type=ChunkType.TOOL_CALL_DELTA, index=0,
                tool_call_id="c1", tool_call_name="subagent",
                arguments_delta='{"prompt": "go and find out"}',
            )
            yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "tool-calls"})
            return
        yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text="done")
        yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=provider)


async def test_cancelling_the_parents_turn_stops_the_child():
    """I4 — a real parent turn, a real child, and a real cancellation.

    The reference never passes the caller's signal down, so the child runs to
    completion and returns its answer into a turn that ended minutes ago.
    """
    child_adapter = SlowAnswerer()
    parent_adapter = Delegator()

    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(LlmService)
    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    await root.plugin(AgentRegistry)
    await root.plugin(AgentLoop)
    await root.plugin(SubagentTool, {"provider": "child", "model": "c-1"})
    root.llm.register_adapter(["parent"], parent_adapter)
    root.llm.register_adapter(["child"], child_adapter)

    session = root.sessions.create("chat-1")
    parent = root.agent_loop.create_agent(
        session, AgentOptions(provider="parent", model="p-1")
    )

    running = asyncio.ensure_future(parent.run("delegate this"))
    await asyncio.wait_for(child_adapter.started.wait(), timeout=2)
    assert parent.activity is not None, "a running turn must expose its signal"

    assert child_adapter.signal is not None, "the child got no signal at all"

    parent.cancel("the user pressed stop")
    await asyncio.wait_for(running, timeout=3)

    assert child_adapter.signal.aborted, "the parent's stop did not reach the child"
    assert not child_adapter.finished, "the child outlived the turn that wanted it"


async def test_an_idle_parent_has_no_activity_signal():
    """`activity` is the turn's scope, so it is None between turns."""
    root, _ = await build()
    assert parent_of(root).activity is None


# --------------------------------------------------------------------------- #
# R2.5 — the scratch session is released
# --------------------------------------------------------------------------- #
async def test_the_child_session_is_released_on_success():
    root, _ = await build()
    parent = parent_of(root)
    before = {s.id for s in root.sessions.list()}
    await call(root, parent)
    assert {s.id for s in root.sessions.list()} == before


async def test_the_child_session_is_released_when_the_child_fails():
    class Exploder(Answerer):
        async def stream(self, options):
            raise RuntimeError("the provider fell over")
            yield  # pragma: no cover - unreachable, keeps this a generator

    root, _ = await build(Exploder())
    parent = parent_of(root)
    before = {s.id for s in root.sessions.list()}
    result = await call(root, parent)
    assert {s.id for s in root.sessions.list()} == before
    assert result.ok  # the failure came back as text, not an exception


# --------------------------------------------------------------------------- #
# R2.6, R2.7 — bounded output and configuration
# --------------------------------------------------------------------------- #
async def test_a_long_answer_is_trimmed_and_says_so():
    root, _ = await build(Answerer("x" * 5000), config={"max_output_bytes": 100})
    result = await call(root, parent_of(root))
    text = str(result.value)
    assert len(text) < 400
    assert "omitted" in text.lower()


async def test_a_short_answer_carries_no_notice():
    root, _ = await build(Answerer("short"))
    result = await call(root, parent_of(root))
    assert str(result.value) == "short"


async def test_a_child_that_says_nothing_says_so():
    class Silent(Answerer):
        async def stream(self, options):
            yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})

    root, _ = await build(Silent())
    result = await call(root, parent_of(root))
    assert "produced no text" in str(result.value)


async def test_a_missing_model_names_what_is_unset():
    root, _ = await build(config={"model": ""})
    result = await call(root, parent_of(root))
    assert "no model configured" in str(result.value)


async def test_a_missing_provider_and_model_names_both():
    root, _ = await build(config={"provider": "", "model": ""})
    result = await call(root, parent_of(root))
    assert "provider and model" in str(result.value)


async def test_branch_depth_of_a_plain_agent_is_zero():
    root, _ = await build()
    assert branch_depth(parent_of(root)) == 0
    assert branch_depth(None) == 0
