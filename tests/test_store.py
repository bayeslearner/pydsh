"""SessionStore lifecycle + durability — Requirements 2 and 3.

Uses a real plugkit Context so mount/lookup/broadcast and the SQLite
round-trip (create -> append -> flush -> load -> derive, the MVP proof) are
exercised end to end.
"""

from __future__ import annotations

import asyncio

import pytest

from plugkit import Context

from pydsh.session import (
    Session,
    SessionError,
    SessionFormatUnsupportedError,
    SessionStore,
    SqliteSessionPersistence,
)

pytestmark = pytest.mark.asyncio


async def test_store_mounts_and_resolves():
    root = Context()
    await root.plugin(SessionStore)
    assert root.sessions is not None
    assert "sessions" in root


async def test_create_get_list():
    root = Context()
    await root.plugin(SessionStore)
    s1 = root.sessions.create("a")
    s2 = root.sessions.create("b")
    assert root.sessions.get("a") is s1
    assert root.sessions.get("nope") is None
    assert {s.id for s in root.sessions.list()} == {"a", "b"}


async def test_duplicate_create_rejected():
    root = Context()
    await root.plugin(SessionStore)
    root.sessions.create("a")
    with pytest.raises(SessionError):
        root.sessions.create("a")


async def test_append_broadcasts_session_event():
    root = Context()
    seen = []
    root.on("session/event", lambda session, event: seen.append((session.id, event.type)))
    await root.plugin(SessionStore)
    s = root.sessions.create("a")
    s.append("user/message", {"content": "hi", "role": "user", "source": {}})
    assert ("a", "user/message") in seen


async def test_session_disposed_with_creating_plugin():
    """A session created inside a plugin is removed when that plugin unloads."""
    from plugkit import plugin as pplugin

    root = Context()
    await root.plugin(SessionStore)
    holder = {}

    @pplugin(inject=["sessions"])
    def make_app(ctx, config=None):
        holder["session"] = ctx.sessions.create("inner")

    fiber = await root.plugin(make_app)
    assert root.sessions.get("inner") is holder["session"]
    await fiber.dispose()
    assert root.sessions.get("inner") is None


async def test_sqlite_round_trip(tmp_path):
    """The MVP proof: a session survives a process restart."""
    db = str(tmp_path / "sessions.db")
    root = Context()
    await root.plugin(SessionStore)
    root.sessions.attach_persistence(SqliteSessionPersistence(db))
    s = root.sessions.create("s1")
    s.append("turn/start", {"turn": 1})
    s.append("user/message", {"content": "hello world", "role": "user", "source": {}})
    s.append("assistant/message", {
        "turn": 1, "step": 1,
        "message": {"content": "hi", "role": "assistant"},
    })
    await root.sessions.flush(s)

    # A fresh runtime is a fresh process: new context, new backend, load.
    backend = SqliteSessionPersistence(db)
    loaded = await backend.load("s1")
    assert loaded is not None
    assert [e.type for e in loaded.events] == [
        "turn/start", "user/message", "assistant/message",
    ]
    assert loaded.derive_messages() == s.derive_messages()
    msgs = loaded.derive_messages()
    assert msgs[0]["content"] == "hello world"
    assert msgs[1]["content"] == "hi"


async def test_load_missing_returns_none(tmp_path):
    backend = SqliteSessionPersistence(str(tmp_path / "s.db"))
    assert await backend.load("missing") is None


async def test_version_mismatch_refuses(tmp_path):
    db = str(tmp_path / "s.db")
    backend = SqliteSessionPersistence(db)
    root = Context()
    await root.plugin(SessionStore)
    root.sessions.attach_persistence(backend)
    s = root.sessions.create("v1")
    s.append("user/message", {"content": "x", "role": "user", "source": {}})
    await root.sessions.flush(s)

    # Corrupt the stored version out-of-band and expect refusal on load.
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("UPDATE sessions SET version = 999 WHERE id = 'v1'")
    conn.commit()
    conn.close()
    with pytest.raises(SessionFormatUnsupportedError):
        await backend.load("v1")


async def test_failing_observer_cannot_break_a_committed_append():
    """The append feed is post-commit — a throwing observer must not undo it.

    `docs/design/data-architecture.md` states observer failures on
    `session/event` are logged and contained. plugkit's `emit` propagates, so
    this is the guard that keeps code and the anchor doc agreeing.
    """
    root = Context()
    await root.plugin(SessionStore)
    session = root.sessions.create("s")

    seen = []
    root.on("session/event", lambda s, e: (_ for _ in ()).throw(RuntimeError("boom")))
    root.on("session/event", lambda s, e: seen.append(e.seq))

    event = session.append("turn/start", {"turn": 1})

    assert event.seq == 1
    assert session.events[0] is event
    assert seen == [1]  # the later observer still ran


async def test_a_flush_only_marks_what_it_actually_wrote(tmp_path):
    """A live session can grow *during* a flush, and the watermark must not
    claim those events.

    Recording `session.seq` after the write marks everything appended in the
    meantime as persisted. The next incremental flush then skips them and they
    are lost — silently, because nothing in the path errors. Found by the
    projection cache, whose forced checkpoints flush concurrently with appends.
    """
    class _Ctx:
        def emit(self, *args, **kwargs):
            pass

    backend = SqliteSessionPersistence(str(tmp_path / "race.db"))
    session = Session(_Ctx(), id="s1")
    session.append("turn/start", {"turn": 1})

    original = backend._write_unsaved

    def append_during_the_write(target):
        # Simulates an append landing while the write is off on its thread.
        written = original(target)
        target.append("turn/start", {"turn": 2})
        return written

    backend._write_unsaved = append_during_the_write
    await backend.flush(session)

    backend._write_unsaved = original
    await backend.flush(session)

    tail = await backend.read_from("s1", 1)
    assert [e.seq for e in tail["events"]] == [1, 2]
