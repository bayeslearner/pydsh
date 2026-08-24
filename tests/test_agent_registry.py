"""The registry and the swappable loop — Requirement 6.

The claim is that the loop is a plugin: callers name ``ctx.agents`` and never
the implementation, so replacing it is a mounting decision. These tests replace
it, and check that unmounting really does end the agents it created.
"""

from __future__ import annotations

import asyncio

from typing import AsyncIterator

import pytest

from plugkit import Context

from pydsh.agent import Agent, AgentLoop, AgentOptions, AgentRegistry
from pydsh.cancel import CancelSignal
from pydsh.llm import (
    ChunkType,
    GenerateOptions,
    LlmAdapter,
    LlmProviderInfo,
    LlmService,
    StreamChunk,
)
from pydsh.session import SessionStore, SqliteSessionPersistence

pytestmark = pytest.mark.asyncio


class Quiet(LlmAdapter):
    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text="ok")
        yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=provider)


async def settle() -> None:
    """Let the kernel finish an activation or a teardown.

    plugkit runs both across more than one event-loop turn, so a test that
    checks the result immediately after `plugin()`/`dispose()` is reading a
    half-finished state rather than a wrong one.
    """
    for _ in range(4):
        await asyncio.sleep(0)


async def mounted(with_loop: bool = True) -> Context:
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(LlmService)
    root.llm.register_adapter(["acme"], Quiet())
    await root.plugin(AgentRegistry)
    if with_loop:
        await root.plugin(AgentLoop)
    return root


# --------------------------------------------------------------------------- #
# The indirection (R6.1–R6.4)
# --------------------------------------------------------------------------- #
async def test_the_registry_mounts_on_its_own():
    root = await mounted(with_loop=False)
    assert root.agents is not None
    assert root.agents.has_factory() is False


async def test_creating_without_a_factory_says_how_to_fix_it():
    """R6.2 — the error names the mount, not just the absence."""
    root = await mounted(with_loop=False)
    session = root.sessions.create()
    with pytest.raises(RuntimeError, match="AgentLoop"):
        root.agents.create_agent(session)


async def test_the_loop_registers_itself_as_the_factory():
    """R6.3"""
    root = await mounted()
    assert root.agents.has_factory() is True
    session = root.sessions.create()
    agent = root.agents.create_agent(session, AgentOptions(provider="acme", model="a-1"))
    assert isinstance(agent, Agent)
    assert agent.id == session.id


async def test_the_loop_does_not_activate_without_its_requirements():
    """R6.4 — the kernel gates activation on `inject`, so a loop mounted
    without the registry (or without an LLM seam) never comes up half-working.
    """
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(AgentLoop)
    assert "agent_loop" not in root

    # And once the missing pieces arrive, it activates by itself.
    await root.plugin(LlmService)
    await root.plugin(AgentRegistry)
    await settle()
    assert "agent_loop" in root
    assert root.agents.has_factory() is True


async def test_a_replacement_factory_takes_over():
    """R6.1 — this is what "the loop is replaceable" means in practice."""
    root = await mounted()

    class Custom:
        def __init__(self) -> None:
            self.built: list = []

        def create_agent(self, session, options=None, **kwargs):
            self.built.append(session.id)
            return "a custom agent"

    custom = Custom()
    root.agents.set_factory(custom)
    session = root.sessions.create()
    assert root.agents.create_agent(session) == "a custom agent"
    assert custom.built == [session.id]


# --------------------------------------------------------------------------- #
# Bookkeeping (R6.5)
# --------------------------------------------------------------------------- #
async def test_get_and_roots():
    root = await mounted()
    first = root.agents.create_agent(root.sessions.create())
    second = root.agents.create_agent(root.sessions.create())
    assert root.agent_loop.get(first.id) is first
    assert root.agent_loop.get("no-such-session") is None
    assert set(root.agent_loop.roots()) == {first, second}


# --------------------------------------------------------------------------- #
# Resume (R6.6) and teardown (R6.7)
# --------------------------------------------------------------------------- #
async def test_resume_rebuilds_a_persisted_session(tmp_path):
    """R6.6 — the loop reaches persistence through ctx.sessions, not past it."""
    db = str(tmp_path / "resume.db")
    writer = await mounted()
    writer.sessions.attach_persistence(SqliteSessionPersistence(db))
    original = writer.sessions.create("chat-1")
    agent = writer.agents.create_agent(
        original, AgentOptions(provider="acme", model="a-1")
    )
    await agent.run("remember this")
    await writer.sessions.flush(original)

    reader = await mounted()
    reader.sessions.attach_persistence(SqliteSessionPersistence(db))
    resumed = await reader.agent_loop.resume("chat-1")

    assert resumed.source == "resume"
    assert [e.type for e in resumed.session.events] == [
        e.type for e in original.events
    ]


async def test_resuming_a_live_session_returns_it_rather_than_reloading(tmp_path):
    """Reloading would discard everything appended since the last flush."""
    root = await mounted()
    root.sessions.attach_persistence(SqliteSessionPersistence(str(tmp_path / "s.db")))
    live = root.sessions.create("chat-1")
    await root.sessions.flush(live)
    live.append("turn/start", {"turn": 99})  # not flushed

    resumed = await root.sessions.resume("chat-1")
    assert resumed is live
    assert [e.type for e in resumed.events][-1] == "turn/start"


async def test_resume_without_persistence_says_what_is_missing():
    root = await mounted()
    with pytest.raises(Exception, match="no persistence backend"):
        await root.sessions.resume("chat-1")


async def test_resume_of_an_unknown_session_is_an_error(tmp_path):
    root = await mounted()
    root.sessions.attach_persistence(SqliteSessionPersistence(str(tmp_path / "s.db")))
    with pytest.raises(Exception, match="no persisted session"):
        await root.sessions.resume("never-existed")


async def test_a_caller_signal_reaches_the_agent():
    """R6.6 — the caller's own teardown ends the agent it asked for."""
    root = await mounted()
    caller = CancelSignal()
    agent = root.agents.create_agent(
        root.sessions.create(), AgentOptions(provider="acme", model="a-1"),
        signal=caller,
    )
    caller.abort("the caller went away")
    await agent.run("go")
    # The lifetime is aborted, so the drain stopped before opening a turn.
    assert [e.type for e in agent.session.events].count("turn/start") == 0


async def test_unmounting_the_loop_ends_its_agents():
    """R6.7 — agents must not keep running against a context with no loop."""
    root = await mounted(with_loop=False)
    fiber = await root.plugin(AgentLoop)
    agent = root.agents.create_agent(
        root.sessions.create(), AgentOptions(provider="acme", model="a-1")
    )
    await agent.run("first")
    assert [e.type for e in agent.session.events].count("turn/start") == 1

    fiber.dispose()
    await settle()

    await agent.run("second")
    assert [e.type for e in agent.session.events].count("turn/start") == 1


async def test_unmounting_leaves_the_registry_without_a_factory():
    """R6.7 — and the next create_agent says so instead of half-working."""
    root = await mounted(with_loop=False)
    fiber = await root.plugin(AgentLoop)
    assert root.agents.has_factory() is True

    fiber.dispose()
    await settle()
    with pytest.raises(RuntimeError, match="no agent factory"):
        root.agents.create_agent(root.sessions.create())
