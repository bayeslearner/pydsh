"""The projection cache — Requirements 1 and 2, properties 1 and 2.

The storage seam's first real consumer, over a real SQLite log and a real
storage domain. The tests that matter most are the ones about *not* trusting a
row: an identity from another lifetime, a row the registry refuses, and the
crash-shaped case where the cache is behind the log.
"""

from __future__ import annotations

import pytest

from plugkit import Context

from pydsh.session import (
    ProjectionCache,
    ProjectionDefinition,
    SessionProjections,
    SessionStore,
    SqliteSessionPersistence,
)
from pydsh.storage import JsonStorage, Storage, StorageDomain

pytestmark = pytest.mark.asyncio


def counter(key: str = "count", version: int = 1):
    return ProjectionDefinition(
        key=key,
        init=lambda: {"n": 0},
        apply=lambda s, e: {"n": s["n"] + 1} if e.type == "turn/start" else s,
        view=lambda s: s["n"],
        state_version=version,
    )


async def build(tmp_path, name: str = "a", with_units: bool = True) -> Context:
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(SessionProjections)
    await root.plugin(Storage)
    await root.plugin(JsonStorage, {"root": str(tmp_path / f"{name}-store")})
    await root.plugin(StorageDomain)
    root.sessions.attach_persistence(SqliteSessionPersistence(str(tmp_path / "log.db")))
    await root.plugin(ProjectionCache, {"write_every_events": 100})
    await root.projection_cache.start()
    if with_units:
        root.session_projections.register(counter())
    return root


def turns(session, n: int, start: int = 1) -> None:
    for i in range(start, start + n):
        session.append("turn/start", {"turn": i})
        session.append("turn/end", {"turn": i, "reason": {"kind": "completed"}})


# --------------------------------------------------------------------------- #
# R1 — reading a tail
# --------------------------------------------------------------------------- #
async def test_read_from_returns_the_header_and_a_tail(tmp_path):
    root = await build(tmp_path)
    session = root.sessions.create("chat-1")
    turns(session, 3)
    await root.sessions.flush(session)

    tail = await root.sessions.persistence.read_from("chat-1", 3)
    assert [e.seq for e in tail["events"]] == [3, 4, 5, 6]
    assert tail["meta"].id == "chat-1"


async def test_read_from_an_absent_session_is_none(tmp_path):
    root = await build(tmp_path)
    assert await root.sessions.persistence.read_from("never", 1) is None


async def test_read_from_past_the_end_gives_the_header_and_nothing(tmp_path):
    root = await build(tmp_path)
    session = root.sessions.create("chat-1")
    turns(session, 1)
    await root.sessions.flush(session)

    tail = await root.sessions.persistence.read_from("chat-1", 999)
    assert tail["events"] == []
    assert tail["meta"].id == "chat-1"


# --------------------------------------------------------------------------- #
# R2.2, R2.3 — the zero-I/O rung
# --------------------------------------------------------------------------- #
async def test_cached_snapshot_is_none_before_anything_is_written(tmp_path):
    root = await build(tmp_path)
    session = root.sessions.create("chat-1")
    assert root.projection_cache.cached_snapshot(session.header) is None


async def test_cached_snapshot_serves_stored_rows(tmp_path):
    root = await build(tmp_path)
    session = root.sessions.create("chat-1")
    turns(session, 4)
    await root.projection_cache.write(session)

    cached = root.projection_cache.cached_snapshot(session.header)
    assert cached["values"] == {"count": 4}
    assert cached["as_of_seq"] == 8


async def test_cached_snapshot_refuses_rows_from_another_lifetime(tmp_path):
    """I2 — an id reused after a rebuild looks perfectly well-formed."""
    root = await build(tmp_path)
    session = root.sessions.create("chat-1")
    turns(session, 2)
    await root.projection_cache.write(session)

    reborn = type(session.header)(id="chat-1", created_at=999.0)
    assert root.projection_cache.cached_snapshot(reborn) is None


async def test_cached_snapshot_omits_a_row_from_another_state_version(tmp_path):
    root = await build(tmp_path)
    session = root.sessions.create("chat-1")
    turns(session, 2)
    await root.projection_cache.write(session)

    root.session_projections.register(counter("other", version=2))
    # `count` is still v1 and readable; a v2 row for it would not be.
    assert root.projection_cache.cached_snapshot(session.header)["values"] == {
        "count": 2
    }


# --------------------------------------------------------------------------- #
# R2.4–R2.7 — the cold read
# --------------------------------------------------------------------------- #
async def test_a_cold_read_replays_only_the_tail(tmp_path):
    root = await build(tmp_path)
    session = root.sessions.create("chat-1")
    turns(session, 2)
    await root.projection_cache.write(session)  # rows cover seq 1..4

    turns(session, 3, start=3)  # three more turns, uncached
    await root.sessions.flush(session)

    snapshot = await root.projection_cache.cold_snapshot("chat-1")
    assert snapshot["values"]["count"] == 5


async def test_a_cold_read_with_no_rows_folds_the_whole_log(tmp_path):
    root = await build(tmp_path)
    session = root.sessions.create("chat-1")
    turns(session, 3)
    await root.sessions.flush(session)

    snapshot = await root.projection_cache.cold_snapshot("chat-1")
    assert snapshot["values"]["count"] == 3


async def test_a_cold_read_writes_the_refreshed_rows_back(tmp_path):
    root = await build(tmp_path)
    session = root.sessions.create("chat-1")
    turns(session, 3)
    await root.sessions.flush(session)

    await root.projection_cache.cold_snapshot("chat-1")
    assert root.projection_cache.cached_snapshot(session.header)["values"] == {
        "count": 3
    }


async def test_a_cold_read_of_an_unpersisted_session_raises(tmp_path):
    """R2.7 — answering for a session that was never stored would be a lie."""
    root = await build(tmp_path)
    with pytest.raises(LookupError, match="no persisted session"):
        await root.projection_cache.cold_snapshot("never-existed")


async def test_a_cold_read_with_no_units_still_proves_the_session_exists(tmp_path):
    root = await build(tmp_path, with_units=False)
    with pytest.raises(LookupError):
        await root.projection_cache.cold_snapshot("never-existed")

    session = root.sessions.create("chat-1")
    turns(session, 1)
    await root.sessions.flush(session)
    assert (await root.projection_cache.cold_snapshot("chat-1"))["values"] == {}


async def test_rows_from_another_lifetime_are_discarded_and_the_log_re_read(tmp_path):
    """R2.5 (I2) — the identity witness, on the cold path."""
    root = await build(tmp_path)
    session = root.sessions.create("chat-1")
    turns(session, 5)
    await root.projection_cache.write(session)

    # Corrupt the stored identity: the rows now describe a different lifetime.
    table = root.projection_cache._domain.table("sessions")
    stored = dict(table.get("chat-1"))
    stored["identity"] = {"id": "chat-1", "created_at": 999.0, "version": 0}
    await table.put("chat-1", stored)

    snapshot = await root.projection_cache.cold_snapshot("chat-1")
    assert snapshot["values"]["count"] == 5  # refolded from the log, not the rows


async def test_a_row_the_registry_refuses_falls_back_to_a_full_re_read(tmp_path):
    """R2.6 — spec 05's refusal to fold over a partial log, handled not propagated."""
    root = await build(tmp_path)
    session = root.sessions.create("chat-1")
    turns(session, 4)
    await root.projection_cache.write(session)

    # A row claiming to have seen far more than the log holds: `restore` refuses
    # it as ahead of the evidence.
    table = root.projection_cache._domain.table("sessions")
    stored = dict(table.get("chat-1"))
    stored["rows"] = {"count": {"ver": 1, "seq": 9999, "val": {"n": 9999}}}
    await table.put("chat-1", stored)

    snapshot = await root.projection_cache.cold_snapshot("chat-1")
    assert snapshot["values"]["count"] == 4


# --------------------------------------------------------------------------- #
# Property 1 — the cache never leads the log
# --------------------------------------------------------------------------- #
async def test_the_write_flushes_the_log_before_storing_the_rows(tmp_path):
    """R2.8 (I1) — the ordering the whole design rests on."""
    order: list[str] = []
    root = await build(tmp_path)

    backend = root.sessions.persistence
    original_flush = backend.flush

    async def watched_flush(session):
        order.append("log")
        await original_flush(session)

    backend.flush = watched_flush

    table = root.projection_cache._domain.table("sessions")
    original_put = table.put

    async def watched_put(key, value):
        order.append("rows")
        await original_put(key, value)

    table.put = watched_put

    session = root.sessions.create("chat-1")
    turns(session, 1)
    # The turn boundary forces its own checkpoint; let it finish and watch a
    # single explicit write, so the order observed is one call's.
    await root.projection_cache.drain()
    order.clear()

    await root.projection_cache.write(session)
    assert order == ["log", "rows"]


async def test_a_cache_behind_the_log_still_reads_correctly(tmp_path):
    """The crash-shaped case: rows written, then more turns, then no checkpoint.

    This is what I1 buys. The cache lags, the cold read replays a longer tail,
    and the answer is the one the log implies.
    """
    root = await build(tmp_path)
    session = root.sessions.create("chat-1")
    turns(session, 2)
    await root.projection_cache.write(session)  # cache knows about 2 turns

    turns(session, 4, start=3)  # four more, flushed to the log but not cached
    await root.sessions.flush(session)

    assert root.projection_cache.cached_snapshot(session.header)["values"] == {"count": 2}
    assert (await root.projection_cache.cold_snapshot("chat-1"))["values"] == {"count": 6}


# --------------------------------------------------------------------------- #
# R2.9, R2.10 — throttling and fail-soft
# --------------------------------------------------------------------------- #
async def test_a_turn_boundary_forces_a_write(tmp_path):
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(SessionProjections)
    await root.plugin(Storage)
    await root.plugin(JsonStorage, {"root": str(tmp_path / "store")})
    await root.plugin(StorageDomain)
    root.sessions.attach_persistence(SqliteSessionPersistence(str(tmp_path / "log.db")))
    await root.plugin(ProjectionCache)
    await root.projection_cache.start()
    root.session_projections.register(counter())

    session = root.sessions.create("chat-1")
    turns(session, 1)
    await root.projection_cache.drain()

    assert root.projection_cache.cached_snapshot(session.header)["values"] == {
        "count": 1
    }


async def test_a_failing_checkpoint_is_logged_and_does_not_propagate(tmp_path, caplog):
    """R2.10 (I3) — the cache stays stale and heals; the caller never knows."""
    root = await build(tmp_path)
    session = root.sessions.create("chat-1")

    table = root.projection_cache._domain.table("sessions")

    async def refuse(key, value):
        raise RuntimeError("the store said no")

    table.put = refuse

    with caplog.at_level("WARNING", logger="pydsh.session.cache"):
        await root.projection_cache._write_soft(session, "test")

    assert any("checkpoint failed" in r.message for r in caplog.records)
