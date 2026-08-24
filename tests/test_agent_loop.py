"""The turn/step loop on a real kernel — Requirement 3 and properties 1 and 4.

Everything here runs on a real plugkit ``Context`` with the real session store
and the real LLM seam. Only the *provider* is scripted, because that is the one
boundary a test cannot own. Mocking the kernel would prove the loop composes
with a mock, which is not the claim being made.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from plugkit import Context

from pydsh.agent import Agent, AgentOptions, PRE_STEP, REQUEST_ERROR, STATUS
from pydsh.llm import (
    ChunkType,
    GenerateOptions,
    LlmAdapter,
    LlmError,
    LlmProviderInfo,
    LlmService,
    StreamChunk,
)
from pydsh.message import MessageSource, TextBlock, create_user_message
from pydsh.session import SessionStore

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# A scripted provider
# --------------------------------------------------------------------------- #
def text_reply(text: str, finish: str = "stop") -> list[StreamChunk]:
    """One plain text answer."""
    return [
        StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text=text),
        StreamChunk(type=ChunkType.FINISH, finish={"kind": finish}),
    ]


def tool_reply(*calls: tuple[str, str, str]) -> list[StreamChunk]:
    """A reply asking for tool calls, as ``(id, name, arguments)`` triples."""
    chunks: list[StreamChunk] = []
    for index, (call_id, name, arguments) in enumerate(calls):
        chunks.append(
            StreamChunk(
                type=ChunkType.TOOL_CALL_DELTA,
                index=index,
                tool_call_id=call_id,
                tool_call_name=name,
                arguments_delta=arguments,
            )
        )
    chunks.append(StreamChunk(type=ChunkType.FINISH, finish={"kind": "tool-calls"}))
    return chunks


class Scripted(LlmAdapter):
    """Replays one scripted turn per call, and records what it was asked."""

    def __init__(self, *replies: list[StreamChunk]) -> None:
        self.replies = list(replies)
        self.calls: list[GenerateOptions] = []
        self.hooks: list = []  # optional per-call coroutine, awaited before yielding

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        index = len(self.calls)
        self.calls.append(options)
        if index < len(self.hooks) and self.hooks[index] is not None:
            await self.hooks[index]()
        reply = self.replies[min(index, len(self.replies) - 1)]
        for chunk in reply:
            yield chunk

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=provider)


class Failing(LlmAdapter):
    """Fails the first ``times`` calls, then replays a scripted answer."""

    def __init__(self, times: int, reply: list[StreamChunk], code: str = "SERVER") -> None:
        self.times = times
        self.reply = reply
        self.code = code
        self.attempts = 0

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        self.attempts += 1
        if self.attempts <= self.times:
            raise LlmError("scripted failure", code=self.code)
            yield  # pragma: no cover - makes this an async generator
        for chunk in self.reply:
            yield chunk

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=provider)


async def build(adapter: LlmAdapter, **option_kwargs) -> tuple[Context, Agent]:
    """A mounted context and an agent on a fresh session."""
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(LlmService)
    root.llm.register_adapter(["acme"], adapter)
    session = root.sessions.create()
    options = AgentOptions(provider="acme", model="a-1", **option_kwargs)
    return root, Agent(root, session, options)


def types(agent: Agent) -> list[str]:
    return [e.type for e in agent.session.events]


def turn_end_reason(agent: Agent) -> dict:
    ends = [e for e in agent.session.events if e.type == "turn/end"]
    assert ends, "the turn never closed"
    return ends[-1].data["reason"]


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
async def test_one_turn_writes_the_whole_story():
    """R3.2, R3.3, R3.6 — the log alone explains what happened (I2)."""
    root, agent = await build(Scripted(text_reply("hello there")))
    await agent.run("hi")

    assert types(agent) == [
        "agent/inbox/spliced",  # delivered
        "agent/inbox/spliced",  # claimed
        "turn/start",
        "user/message",
        "step/start",
        "assistant/chunk",
        "assistant/chunk",
        "assistant/message",
        "step/end",
        "turn/end",
    ]
    assert turn_end_reason(agent) == {"kind": "completed"}


async def test_the_model_sees_the_derived_history():
    """The request is built from the log, not from what the caller passed."""
    adapter = Scripted(text_reply("first"), text_reply("second"))
    root, agent = await build(adapter)
    await agent.run("one")
    await agent.run("two")

    assert len(adapter.calls) == 2
    second_call = [m.content[0].text for m in adapter.calls[1].messages]
    assert second_call == ["one", "first", "two"]


async def test_turn_numbers_continue_from_the_log():
    """R3.2 — a second turn does not reuse the first turn's number."""
    root, agent = await build(Scripted(text_reply("a"), text_reply("b")))
    await agent.run("one")
    await agent.run("two")
    starts = [e.data["turn"] for e in agent.session.events if e.type == "turn/start"]
    assert starts == [1, 2]


async def test_status_brackets_the_drain():
    """R3.13 — a consumer can tell when a run began and ended."""
    root, agent = await build(Scripted(text_reply("hi")))
    seen: list[str] = []
    root.on(STATUS, lambda payload: seen.append(payload["status"]))
    await agent.run("hi")
    assert seen == ["running", "idle"]


async def test_the_route_lands_on_the_session_header():
    """R7.1 — the epoch's call config is recorded where a resume can read it."""
    root, agent = await build(Scripted(text_reply("hi")))
    await agent.run("hi")
    assert agent.session.header.request == {"provider": "acme", "model": "a-1"}


# --------------------------------------------------------------------------- #
# Every way a turn can end (property 1)
# --------------------------------------------------------------------------- #
async def test_step_budget_ends_the_turn_as_max_steps():
    """R3.7 — and it says max-steps, not max-tokens: different failures."""
    adapter = Scripted(tool_reply(("c1", "echo", "{}")))
    root, agent = await build(adapter, max_steps=2)

    class Echo:
        name = "echo"
        description = "echoes"
        parameters: dict = {}

        async def execute(self, arguments, execution=None):
            return "echoed"

    from plugkit import PointsService, ToolsService

    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    root.tools.register(Echo())

    await agent.run("go")
    assert turn_end_reason(agent) == {"kind": "max-steps"}
    assert len(adapter.calls) == 2  # never a third model call


async def test_token_ceiling_ends_the_turn_and_keeps_the_reply():
    """R3.8 — a truncated answer is still written; the user should see it."""
    root, agent = await build(Scripted(text_reply("cut off", finish="max-tokens")))
    await agent.run("go")
    assert turn_end_reason(agent) == {"kind": "max-tokens"}
    assert "assistant/message" in types(agent)


async def test_stream_error_ends_the_turn_without_an_assistant_message():
    """R3.9 — there was no reply, so the history must not claim there was."""
    root, agent = await build(Scripted([StreamChunk(type=ChunkType.FINISH,
                                                    finish={"kind": "error"})]))
    await agent.run("go")
    assert turn_end_reason(agent) == {"kind": "error"}
    assert "assistant/message" not in types(agent)


async def test_pre_step_rejection_ends_the_turn_as_blocked():
    """R3.4 — a plugin can refuse the input, and the log says so."""
    root, agent = await build(Scripted(text_reply("never sent")))

    async def refuse(payload, next_):
        return {"kind": "reject"}

    root.on(PRE_STEP, refuse)
    await agent.run("go")
    assert turn_end_reason(agent) == {"kind": "blocked"}
    assert "user/message" not in types(agent)


async def test_turn_closes_even_when_the_step_raises():
    """R3.11 (I1) — the turn is closed *and* the failure still surfaces."""

    class Exploding(LlmAdapter):
        async def stream(self, options):
            raise RuntimeError("boom")
            yield  # pragma: no cover

        def provider_info(self, provider):
            return LlmProviderInfo(id=provider, name=provider)

    root, agent = await build(Exploding())
    with pytest.raises(RuntimeError, match="boom"):
        await agent.run("go")
    assert types(agent)[-1] == "turn/end"
    assert types(agent)[-2] == "step/end"
    # And the log says it failed — not "completed", which would be a lie a
    # later reader has no way to detect.
    assert turn_end_reason(agent) == {"kind": "failed", "error": "RuntimeError: boom"}


# --------------------------------------------------------------------------- #
# Plugin injection and recovery
# --------------------------------------------------------------------------- #
async def test_pre_step_can_inject_context():
    """R3.3 — injected messages become model-visible history, not a system hack."""
    root, agent = await build(Scripted(text_reply("ok")))

    async def inject(payload, next_):
        decision = await next_()
        extra = create_user_message(
            [TextBlock("today is tuesday")], MessageSource("plugin", plugin="clock")
        )
        return {"kind": "enter", "messages": [extra, *decision["messages"]]}

    root.on(PRE_STEP, inject)
    await agent.run("what day is it?")

    texts = [
        e.data["__msg__"]["content"][0]["text"]
        for e in agent.session.events
        if e.type == "user/message"
    ]
    assert texts == ["today is tuesday", "what day is it?"]


async def test_request_error_retry_reruns_the_step():
    """R3.12 — a recovering plugin gets one more attempt from the new surface."""
    adapter = Failing(1, text_reply("recovered"))
    root, agent = await build(adapter)

    async def recover(payload, next_):
        return {"kind": "retry"}

    root.on(REQUEST_ERROR, recover)
    await agent.run("go")

    assert adapter.attempts == 2
    assert turn_end_reason(agent) == {"kind": "completed"}
    # The failed attempt contributed no chunks: a retry is a fresh reply, not a
    # continuation of the one that died.
    chunks = [e for e in agent.session.events if e.type == "assistant/chunk"]
    assert len(chunks) == 2


async def test_unrecovered_request_error_propagates():
    """R3.12 — with no plugin answering `retry`, the failure is not swallowed."""
    root, agent = await build(Failing(1, text_reply("never")))
    with pytest.raises(LlmError):
        await agent.run("go")
    assert types(agent)[-1] == "turn/end"
    assert turn_end_reason(agent)["kind"] == "failed"


# --------------------------------------------------------------------------- #
# Delivery, cancellation, re-entry (property 4)
# --------------------------------------------------------------------------- #
async def test_run_waits_for_a_drain_already_in_flight():
    """R3.1 — the edge case the reference gets wrong: run() must not return early."""
    started = asyncio.Event()
    release = asyncio.Event()
    adapter = Scripted(text_reply("first"), text_reply("second"))

    async def block():
        started.set()
        await release.wait()

    adapter.hooks = [block, None]
    root, agent = await build(adapter)

    agent.insert(create_user_message([TextBlock("one")], MessageSource("user")))
    await started.wait()  # the background drain is inside the first model call

    async def finish_soon():
        await asyncio.sleep(0)
        release.set()

    asyncio.ensure_future(finish_soon())
    await agent.run("two")

    # Both turns are complete by the time run() returns — that is the promise.
    ends = [e for e in agent.session.events if e.type == "turn/end"]
    assert len(ends) == 2


async def test_cancel_ends_the_turn_and_leaves_the_agent_usable():
    """R3.10 + I5 — cancelling is not killing (the reference's defect)."""
    started = asyncio.Event()
    adapter = Scripted(text_reply("interrupted"), text_reply("after"))

    holder: dict = {}

    async def stall():
        started.set()
        holder["agent"].cancel("user stopped")

    adapter.hooks = [stall, None]
    root, agent = await build(adapter)
    holder["agent"] = agent

    await agent.run("one")
    assert turn_end_reason(agent) == {"kind": "cancelled", "reason": "user stopped"}

    # And now the part that matters: the agent still works.
    await agent.run("two")
    assert turn_end_reason(agent) == {"kind": "completed"}


async def test_cancel_before_the_drain_starts_is_not_lost():
    """A deliver-then-cancel pair must not fall between the two moments."""
    root, agent = await build(Scripted(text_reply("never")))
    agent.insert(create_user_message([TextBlock("one")], MessageSource("user")))
    agent.cancel("changed my mind")
    await agent.when_idle()

    # The drain aborted at its first checkpoint, so no turn ever opened.
    assert "turn/start" not in types(agent)


async def test_dispose_ends_the_agent_for_good():
    """I5's other half — a lifetime abort really does stop it."""
    root, agent = await build(Scripted(text_reply("never")))
    agent.dispose()
    await agent.run("go")
    assert "turn/start" not in types(agent)
