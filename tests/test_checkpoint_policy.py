"""Automatic durability — Requirement 5.

Until this policy, a conversation reached disk only if the consumer remembered
to call `flush`, and the agent loop never does. These tests are about the gap
between "durable" as a capability and "durable" as something that happens.
"""

from __future__ import annotations

import asyncio

import pytest

from plugkit import Context

from pydsh.session import (
    CheckpointPolicy,
    SessionStore,
    SqliteSessionPersistence,
)

pytestmark = pytest.mark.asyncio


class CountingBackend:
    """A persistence backend that records what it was asked to write."""

    def __init__(self, fail: bool = False) -> None:
        self.flushes = 0
        self.fail = fail

    async def create(self, session) -> None:
        pass

    async def flush(self, session) -> None:
        self.flushes += 1
        if self.fail:
            raise RuntimeError("the disk is full")

    async def load(self, id: str):
        return None

    async def list(self):
        return []


async def mounted(backend=None, **config) -> tuple[Context, object]:
    root = Context()
    await root.plugin(SessionStore)
    if backend is not None:
        root.sessions.attach_persistence(backend)
    await root.plugin(CheckpointPolicy, config)
    return root, root.sessions.create()


def end_turns(session, n: int, start: int = 1) -> None:
    for i in range(start, start + n):
        session.append("turn/start", {"turn": i})
        session.append("turn/end", {"turn": i, "reason": {"kind": "completed"}})


# --------------------------------------------------------------------------- #
# Configuration (R5.2)
# --------------------------------------------------------------------------- #
async def test_a_zero_interval_is_rejected():
    root = Context()
    await root.plugin(SessionStore)
    with pytest.raises(Exception, match="positive"):
        await root.plugin(CheckpointPolicy, {"every_turns": 0})


async def test_a_non_integer_interval_is_rejected():
    root = Context()
    await root.plugin(SessionStore)
    with pytest.raises(Exception, match="integer"):
        await root.plugin(CheckpointPolicy, {"every_turns": 2.5})


# --------------------------------------------------------------------------- #
# Cadence (R5.1)
# --------------------------------------------------------------------------- #
async def test_it_flushes_every_n_turns():
    backend = CountingBackend()
    root, session = await mounted(backend, every_turns=2)

    end_turns(session, 1)
    await root.checkpoint_policy.drain()
    assert backend.flushes == 0  # not yet

    end_turns(session, 1, start=2)
    await root.checkpoint_policy.drain()
    assert backend.flushes == 1

    end_turns(session, 2, start=3)
    await root.checkpoint_policy.drain()
    assert backend.flushes == 2


async def test_only_turn_boundaries_count():
    """Steps are not turns — a long turn is one checkpoint, not many."""
    backend = CountingBackend()
    root, session = await mounted(backend, every_turns=1)

    session.append("turn/start", {"turn": 1})
    for step in range(1, 4):
        session.append("step/start", {"turn": 1, "step": step})
        session.append("step/end", {"turn": 1, "step": step})
    await root.checkpoint_policy.drain()
    assert backend.flushes == 0

    session.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
    await root.checkpoint_policy.drain()
    assert backend.flushes == 1


async def test_sessions_are_counted_separately():
    backend = CountingBackend()
    root, session = await mounted(backend, every_turns=2)
    other = root.sessions.create()

    end_turns(session, 1)
    end_turns(other, 1)
    await root.checkpoint_policy.drain()
    assert backend.flushes == 0  # one turn each, neither has reached two


# --------------------------------------------------------------------------- #
# Resilience (R5.3, R5.4)
# --------------------------------------------------------------------------- #
async def test_no_backend_is_not_a_failure():
    """R5.3 — a composition without durability is a choice, not a bug."""
    root, session = await mounted(every_turns=1)
    end_turns(session, 2)
    await root.checkpoint_policy.drain()  # nothing raised


async def test_attaching_a_backend_later_still_works():
    """Counting continues while nothing is attached, so this is not stuck."""
    root, session = await mounted(every_turns=1)
    end_turns(session, 1)
    backend = CountingBackend()
    root.sessions.attach_persistence(backend)
    end_turns(session, 1, start=2)
    await root.checkpoint_policy.drain()
    assert backend.flushes == 1


async def test_a_failing_flush_is_logged_and_does_not_break_the_turn(caplog):
    """R5.4 — a durability hiccup must not become a failed turn."""
    backend = CountingBackend(fail=True)
    root, session = await mounted(backend, every_turns=1)

    with caplog.at_level("WARNING", logger="pydsh.checkpoint"):
        end_turns(session, 1)
        await root.checkpoint_policy.drain()

    assert any("checkpoint flush failed" in r.message for r in caplog.records)
    # And the log is intact, so the next checkpoint can still write it.
    assert [e.type for e in session.events][-1] == "turn/end"


async def test_a_later_flush_recovers_after_a_failure():
    backend = CountingBackend(fail=True)
    root, session = await mounted(backend, every_turns=1)
    end_turns(session, 1)
    await root.checkpoint_policy.drain()

    backend.fail = False
    end_turns(session, 1, start=2)
    await root.checkpoint_policy.drain()
    assert backend.flushes == 2


# --------------------------------------------------------------------------- #
# The whole point (R5.1) — end to end through SQLite
# --------------------------------------------------------------------------- #
async def test_a_conversation_reaches_disk_without_anyone_calling_flush(tmp_path):
    db = str(tmp_path / "auto.db")
    root = Context()
    await root.plugin(SessionStore)
    root.sessions.attach_persistence(SqliteSessionPersistence(db))
    await root.plugin(CheckpointPolicy, {"every_turns": 1})

    session = root.sessions.create("chat-1")
    end_turns(session, 1)
    await root.checkpoint_policy.drain()

    reader = Context()
    await reader.plugin(SessionStore)
    reader.sessions.attach_persistence(SqliteSessionPersistence(db))
    reloaded = await reader.sessions.resume("chat-1")

    assert [e.type for e in reloaded.events] == ["turn/start", "turn/end"]


async def test_unmounting_stops_the_policy():
    """R5.5"""
    backend = CountingBackend()
    root = Context()
    await root.plugin(SessionStore)
    root.sessions.attach_persistence(backend)
    fiber = await root.plugin(CheckpointPolicy, {"every_turns": 1})
    session = root.sessions.create()

    end_turns(session, 1)
    await root.checkpoint_policy.drain()
    assert backend.flushes == 1

    fiber.dispose()
    for _ in range(4):
        await asyncio.sleep(0)

    end_turns(session, 1, start=2)
    await asyncio.sleep(0)
    assert backend.flushes == 1
