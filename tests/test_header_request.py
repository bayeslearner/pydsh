"""The call-config epoch on the session header — Requirement 7.

Spec 02 shipped the encoding and deferred the write path to "the agent loop
that owns the epoch". This is that path, proven the way it will actually be
used: a step records the route, the session is flushed, and a fresh process
reads it back.

The point is not that a dict survives SQLite. It is that resuming a
conversation continues on the provider and model it was running under, instead
of silently switching to whatever the next caller happens to pass.
"""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from plugkit import Context

from pydsh.agent import Agent, AgentOptions
from pydsh.llm import (
    ChunkType,
    GenerateOptions,
    LlmAdapter,
    LlmProviderInfo,
    LlmService,
    StreamChunk,
)
from pydsh.session import Session, SessionStore, SqliteSessionPersistence

pytestmark = pytest.mark.asyncio


class Quiet(LlmAdapter):
    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text="ok")
        yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=provider)


async def mounted(db: str | None = None) -> Context:
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(LlmService)
    root.llm.register_adapter(["acme"], Quiet())
    if db is not None:
        root.sessions.attach_persistence(SqliteSessionPersistence(db))
    return root


# --------------------------------------------------------------------------- #
# R7.1 — the step records it
# --------------------------------------------------------------------------- #
async def test_a_step_records_the_route():
    root = await mounted()
    session = root.sessions.create()
    agent = Agent(root, session, AgentOptions(provider="acme", model="a-1"))
    assert session.header.request is None

    await agent.run("go")
    assert session.header.request == {"provider": "acme", "model": "a-1"}


async def test_the_recorded_config_carries_the_bounds_too():
    root = await mounted()
    session = root.sessions.create()
    agent = Agent(
        root, session, AgentOptions(provider="acme", model="a-1", max_tokens=512)
    )
    await agent.run("go")
    assert session.header.request == {
        "provider": "acme",
        "model": "a-1",
        "max_tokens": 512,
    }


# --------------------------------------------------------------------------- #
# R7.2 — it survives the round trip
# --------------------------------------------------------------------------- #
async def test_the_route_survives_to_json_and_back():
    root = await mounted()
    session = root.sessions.create()
    session.header.request = {"provider": "acme", "model": "a-1"}
    rebuilt = Session.from_json(root, session.to_json())
    assert rebuilt.header.request == {"provider": "acme", "model": "a-1"}


async def test_an_unset_route_round_trips_as_none():
    root = await mounted()
    session = root.sessions.create()
    rebuilt = Session.from_json(root, session.to_json())
    assert rebuilt.header.request is None


async def test_the_route_survives_sqlite(tmp_path):
    """R7.2 — through the real backend, not just the dataclass."""
    db = str(tmp_path / "route.db")
    writer = await mounted(db)
    session = writer.sessions.create("chat-1")
    agent = Agent(writer, session, AgentOptions(provider="acme", model="a-1"))
    await agent.run("go")
    await writer.sessions.flush(session)

    reader = await mounted(db)
    resumed = await reader.sessions.resume("chat-1")
    assert resumed.header.request == {"provider": "acme", "model": "a-1"}


# --------------------------------------------------------------------------- #
# R7.3 — a resumed session continues on the same route
# --------------------------------------------------------------------------- #
async def test_a_resumed_session_carries_the_last_route(tmp_path):
    """The last step before the flush is what a resume must find."""
    db = str(tmp_path / "route.db")
    writer = await mounted(db)
    session = writer.sessions.create("chat-1")

    first = Agent(writer, session, AgentOptions(provider="acme", model="a-1"))
    await first.run("one")
    await writer.sessions.flush(session)

    # The route changes mid-conversation, as a consumer switching models would.
    second = Agent(writer, session, AgentOptions(provider="acme", model="a-2"))
    await second.run("two")
    await writer.sessions.flush(session)

    reader = await mounted(db)
    resumed = await reader.sessions.resume("chat-1")
    assert resumed.header.request["model"] == "a-2"


async def test_a_header_only_change_is_still_persisted(tmp_path):
    """The flush must not skip the header just because no event followed it.

    Events are the append-only part; the header is mutable state, and an
    incremental flush that writes only new events would drop a route change
    that happened to be the last thing to occur.
    """
    db = str(tmp_path / "header.db")
    writer = await mounted(db)
    session = writer.sessions.create("chat-1")
    session.append("turn/start", {"turn": 1})
    await writer.sessions.flush(session)

    session.header.request = {"provider": "acme", "model": "late"}
    await writer.sessions.flush(session)  # no new events at all

    reader = await mounted(db)
    resumed = await reader.sessions.resume("chat-1")
    assert resumed.header.request == {"provider": "acme", "model": "late"}
