"""The SDK — Requirement 4, property 3.

These are the only tests in the repo that go from *nothing* to an answer. Every
other suite mounts what it needs by hand; this one proves the front door works,
which is what a consumer will actually meet first.
"""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from plugkit import Context, PointsService

from pydsh import (
    AgentOptions,
    ChunkType,
    GenerateOptions,
    Harness,
    LlmAdapter,
    LlmProviderInfo,
    LlmService,
    ProfileError,
    RunResult,
    SessionStore,
    StreamChunk,
    core_profile,
)
from pydsh.boot.harness import HarnessError, final_response

pytestmark = pytest.mark.asyncio

OPTIONS = AgentOptions(provider="acme", model="a-1")


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


async def ready(adapter=None, **kwargs) -> Harness:
    """A started harness with an adapter on the `acme` route."""
    harness = Harness(options=OPTIONS, env={}, **kwargs)
    await harness.start()
    harness.ctx.llm.register_adapter(["acme"], adapter or Answerer())
    return harness


# --------------------------------------------------------------------------- #
# R4.1 — assembly
# --------------------------------------------------------------------------- #
async def test_a_harness_assembles_the_core_profile():
    """R4.1."""
    harness = Harness(env={})
    try:
        ctx = await harness.start()
        for name in ("sessions", "llm", "agents", "agent_loop"):
            assert getattr(ctx, name) is not None
    finally:
        await harness.close()


async def test_start_is_idempotent():
    harness = Harness(env={})
    try:
        assert await harness.start() is await harness.start()
    finally:
        await harness.close()


async def test_nothing_is_mounted_until_start():
    """Mounting is async and can fail; a constructor cannot report which half."""
    harness = Harness(env={})
    assert harness.ctx is None
    await harness.close()


async def test_a_custom_profile_is_used():
    harness = Harness([(PointsService, {}), (SessionStore, {})], env={})
    try:
        ctx = await harness.start()
        assert ctx.sessions is not None
        assert getattr(ctx, "llm", None) is None
    finally:
        await harness.close()


async def test_a_bad_profile_raises_and_mounts_nothing():
    harness = Harness([(None, {})], env={})
    with pytest.raises(ProfileError):
        await harness.start()
    assert harness.ctx is None
    await harness.close()


# --------------------------------------------------------------------------- #
# R4.2, R4.3 — sessions and runs
# --------------------------------------------------------------------------- #
async def test_a_run_returns_the_answer():
    """R4.3."""
    harness = await ready(Answerer("forty-two"))
    try:
        result = await harness.session("chat-1").run("what is it?")
        assert isinstance(result, RunResult)
        assert result.session_id == "chat-1"
        assert result.final_response == "forty-two"
        assert result.session is harness.ctx.sessions.get("chat-1")
    finally:
        await harness.close()


async def test_an_unnamed_session_gets_a_name():
    """R4.2."""
    harness = await ready()
    try:
        first = harness.session()
        second = harness.session()
        assert first.id.startswith("session-") and first.id != second.id
    finally:
        await harness.close()


async def test_the_events_come_back_with_the_result():
    harness = await ready()
    try:
        result = await harness.session("chat-1").run("hello")
        types = [e.type for e in result.events]
        assert "turn/start" in types and "assistant/message" in types
        assert types[-1] == "turn/end"
    finally:
        await harness.close()


async def test_a_second_run_continues_the_same_session():
    harness = await ready(Answerer("first", "second"))
    try:
        session = harness.session("chat-1")
        await session.run("one")
        result = await session.run("two")
        assert result.final_response == "second"
        assert [e.type for e in result.events].count("turn/start") == 2
    finally:
        await harness.close()


async def test_two_sessions_are_separate_conversations():
    harness = await ready()
    try:
        a = await harness.session("chat-a").run("hello")
        b = await harness.session("chat-b").run("hello")
        assert a.session is not b.session
        assert len(b.events) == len(a.events)
    finally:
        await harness.close()


async def test_send_delivers_without_waiting():
    harness = await ready()
    try:
        session = harness.session("chat-1")
        await session.send("go")
        agent = harness.ctx.agent_loop.get("chat-1")
        await agent.when_idle()
        assert final_response(agent.session) == "the answer"
    finally:
        await harness.close()


# --------------------------------------------------------------------------- #
# R4.4 — the answer comes from the log (property 3)
# --------------------------------------------------------------------------- #
async def test_the_result_matches_the_transcript():
    """Property 3 (R4.4) — a result that disagrees is found by nobody."""
    harness = await ready(Answerer("what the model said"))
    try:
        result = await harness.session("chat-1").run("hello")
        assert result.final_response == final_response(result.session)
    finally:
        await harness.close()


async def test_the_last_assistant_message_wins():
    harness = await ready(Answerer("first", "second", "third"))
    try:
        session = harness.session("chat-1")
        await session.run("one")
        await session.run("two")
        result = await session.run("three")
        assert result.final_response == "third"
    finally:
        await harness.close()


async def test_a_session_with_no_assistant_message_answers_with_nothing():
    root = Context()
    await root.plugin(PointsService)
    await root.plugin(SessionStore)
    session = root.sessions.create("empty")
    assert final_response(session) == ""


# --------------------------------------------------------------------------- #
# R4.5, R4.6 — teardown
# --------------------------------------------------------------------------- #
async def test_close_unmounts_everything():
    """R4.5."""
    harness = await ready()
    ctx = harness.ctx
    await harness.close()
    assert harness.ctx is None
    assert getattr(ctx, "sessions", None) is None


async def test_close_is_idempotent():
    harness = await ready()
    await harness.close()
    await harness.close()


async def test_a_closed_harness_cannot_be_restarted():
    harness = await ready()
    await harness.close()
    with pytest.raises(HarnessError):
        await harness.start()


async def test_the_context_manager_tears_down_on_the_happy_path():
    """R4.6, I6."""
    async with Harness(env={}, options=OPTIONS) as harness:
        harness.ctx.llm.register_adapter(["acme"], Answerer())
        ctx = harness.ctx
        result = await harness.session("chat-1").run("hello")
        assert result.final_response == "the answer"
    assert getattr(ctx, "sessions", None) is None


async def test_the_context_manager_tears_down_when_the_turn_raises():
    """I6 — on every path, which is the whole reason it is a context manager."""

    class Exploding(LlmAdapter):
        async def stream(self, options):
            raise RuntimeError("the provider fell over")
            yield  # pragma: no cover

        def provider_info(self, provider):
            return LlmProviderInfo(id=provider, name=provider)

    ctx = None
    with pytest.raises(Exception):
        async with Harness(env={}, options=OPTIONS) as harness:
            harness.ctx.llm.register_adapter(["acme"], Exploding())
            ctx = harness.ctx
            await harness.session("chat-1").run("hello")
    assert ctx is not None
    assert getattr(ctx, "sessions", None) is None


# --------------------------------------------------------------------------- #
# R4.7 — a readable failure
# --------------------------------------------------------------------------- #
async def test_a_run_with_no_adapter_fails_readably():
    """R4.7 — a readable error, not a hang."""
    harness = Harness(env={}, options=OPTIONS)
    try:
        await harness.start()
        with pytest.raises(Exception) as caught:
            await harness.session("chat-1").run("hello")
        assert "acme" in str(caught.value)
    finally:
        await harness.close()


async def test_a_profile_with_no_agent_registry_says_so():
    harness = Harness([(PointsService, {}), (SessionStore, {})], env={})
    try:
        with pytest.raises(HarnessError) as caught:
            await harness.session("chat-1").run("hello")
        assert "no agent registry" in str(caught.value)
    finally:
        await harness.close()


async def test_a_profile_with_no_session_store_says_so():
    from pydsh import AgentLoop, AgentRegistry

    harness = Harness(
        [(PointsService, {}), (LlmService, {}), (AgentRegistry, {})], env={}
    )
    try:
        with pytest.raises(HarnessError) as caught:
            await harness.session("chat-1").run("hello")
        assert "session store" in str(caught.value)
    finally:
        await harness.close()


# --------------------------------------------------------------------------- #
# Environment and home reach the harness
# --------------------------------------------------------------------------- #
async def test_the_layered_environment_reaches_profile_interpolation(tmp_path):
    """The reason the environment is layered before the profile resolves."""
    (tmp_path / ".env").write_text("ACME_MODEL=from-the-file\n")

    class Recorder(PointsService):
        provide = "recorder"

        def __init__(self, ctx, config=None):
            super().__init__(ctx)
            self.seen = (config or {}).get("model")

    harness = Harness(
        [(PointsService, {}), (Recorder, {"model": "${ACME_MODEL}"})],
        cwd=str(tmp_path),
        home=str(tmp_path / "home"),
        env={},
    )
    try:
        ctx = await harness.start()
        assert ctx.recorder.seen == "from-the-file"
    finally:
        await harness.close()


async def test_the_harness_records_its_home(tmp_path):
    harness = Harness(env={}, home=str(tmp_path))
    try:
        assert harness.home == str(tmp_path)
    finally:
        await harness.close()
