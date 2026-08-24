"""The persistence seam and its SQLite backend.

A session's durability is explicit: ``flush`` is the checkpoint. The SQLite
backend writes the header and each event in one transaction per append, WAL
mode, so an acknowledged flush is on disk. Loading rebuilds a ``Session`` from
the ``sessions`` + ``events`` tables and refuses to reconstruct a log whose
format version differs (no migration while unreleased).

The synchronous ``sqlite3`` driver is stepped off the event loop with
``asyncio.to_thread``; the store's ``flush`` awaits this via ``session/flush``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from abc import ABC
from pathlib import Path
from typing import Any, Optional

from .events import SESSION_FORMAT_VERSION
from .session import Session, SessionEvent, SessionError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    version    INTEGER NOT NULL,
    created_at REAL NOT NULL,
    cwd        TEXT,
    -- The call config the last step ran under, as JSON. Header metadata, not
    -- conversation content: a resumed session continues on the same route
    -- rather than silently falling back to whatever the caller passes next.
    request    TEXT
);
CREATE TABLE IF NOT EXISTS events (
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    type       TEXT NOT NULL,
    time       REAL NOT NULL,
    data       TEXT NOT NULL,
    surface_op TEXT,
    source_event_seqs TEXT,
    PRIMARY KEY (session_id, seq),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
"""


def _encode_request(request: Any) -> Optional[str]:
    """The header's call config as the JSON text the ``request`` column holds."""
    return None if request is None else json.dumps(request)


class SessionPersistenceError(SessionError):
    """Persistence backend error."""


class SessionFormatUnsupportedError(SessionPersistenceError):
    """A stored session's format version is not supported."""


class SessionPersistence(ABC):
    """The durability seam — what a backend must provide on top of ``Session``."""

    async def create(self, session: Session) -> None:  # pragma: no cover - ABC
        raise NotImplementedError

    async def flush(self, session: Session) -> None:  # pragma: no cover - ABC
        raise NotImplementedError

    async def load(self, id: str) -> Optional[Session]:  # pragma: no cover - ABC
        raise NotImplementedError

    async def list(self) -> list[str]:  # pragma: no cover - ABC
        raise NotImplementedError


class SqliteSessionPersistence(SessionPersistence):
    """SQLite session-log backend (WAL, one transaction per append)."""

    def __init__(self, db_path: str) -> None:
        self._db_path = str(Path(db_path))
        # Each flushed event is its own transaction; the store holds one live
        # event during a flush, so a per-connection write lock is enough.
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        # Follow the reference's "torn tail is discarded" rule at the index
        # level only via WAL; a committed prefix is never rolled back.
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = asyncio.Lock()
        self._persisted_seq: dict[int, int] = {}

    # -- Session.to_json round-trips through SQL as JSON text ------------

    def _write(self, session: Session) -> None:
        payload = session.to_json()
        header = payload["header"]
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions"
                " (id, version, created_at, cwd, request) VALUES (?, ?, ?, ?, ?)",
                (
                    header["id"],
                    header["version"],
                    header["created_at"],
                    header["cwd"],
                    _encode_request(header.get("request")),
                ),
            )
            for e in payload["events"]:
                self._conn.execute(
                    "INSERT OR REPLACE INTO events"
                    " (session_id, seq, type, time, data, surface_op,"
                    "  source_event_seqs) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        header["id"],
                        e["seq"],
                        e["type"],
                        e["time"],
                        json.dumps(e["data"]),
                        e.get("surface_op"),
                        json.dumps(e.get("source_event_seqs", [])),
                    ),
                )

    def _write_unsaved(self, session: Session) -> None:
        """Append only the events that have not been persisted yet.

        Ensures the header row exists first: a store ``create`` does not
        persist until the first flush, so the ``sessions`` row may be absent
        when a flush arrives.

        The header is written on every flush, not only when events are new.
        Events are the append-only part; the header is mutable state (the call
        config the last step ran under), and skipping the write when no event
        happened to follow it would drop that change on the floor.
        """
        from_seq = self._persisted_seq.get(id(session), 0)
        fresh = [e for e in session.events if e.seq > from_seq]
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions"
                " (id, version, created_at, cwd, request) VALUES (?, ?, ?, ?, ?)",
                (
                    session.id,
                    session.header.version,
                    session.header.created_at,
                    session.header.cwd,
                    _encode_request(session.header.request),
                ),
            )
            for e in fresh:
                self._conn.execute(
                    "INSERT OR REPLACE INTO events"
                    " (session_id, seq, type, time, data, surface_op,"
                    "  source_event_seqs) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        session.id,
                        e.seq,
                        e.type,
                        e.time,
                        json.dumps(e.data),
                        e.surface_op,
                        json.dumps(list(e.source_event_seqs)),
                    ),
                )

    def _read(self, id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT version, created_at, cwd, request FROM sessions WHERE id = ?",
            (id,),
        ).fetchone()
        if row is None:
            return None
        version, created_at, cwd, request = row
        if version != SESSION_FORMAT_VERSION:
            raise SessionFormatUnsupportedError(
                f"session {id!r} has format version {version}, expected "
                f"{SESSION_FORMAT_VERSION}; refusing to load (no migration)"
            )
        rows = self._conn.execute(
            "SELECT type, seq, time, data, surface_op, source_event_seqs"
            " FROM events WHERE session_id = ? ORDER BY seq",
            (id,),
        ).fetchall()
        return {
            "id": id,
            "created_at": created_at,
            "cwd": cwd,
            "request": json.loads(request) if request else None,
            "events": [
                {
                    "type": r[0],
                    "seq": r[1],
                    "time": r[2],
                    "data": json.loads(r[3]),
                    "surface_op": r[4],
                    "source_event_seqs": json.loads(r[5]) if r[5] else [],
                }
                for r in rows
            ],
        }

    # -- SessionPersistence -------------------------------------------------

    async def create(self, session: Session) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write, session)
            self._persisted_seq[id(session)] = session.seq

    async def flush(self, session: Session) -> None:
        # Track the persisted tail so an incremental flush appends only the
        # events that landed since the last checkpoint.
        async with self._lock:
            await asyncio.to_thread(self._write_unsaved, session)
            self._persisted_seq[id(session)] = session.seq

    async def load(self, id: str) -> Optional[Session]:
        payload = await asyncio.to_thread(self._read, id)
        if payload is None:
            return None
        snapshot = {
            "header": {
                "version": SESSION_FORMAT_VERSION,
                "id": payload["id"],
                "created_at": payload["created_at"],
                "cwd": payload["cwd"],
                "request": payload["request"],
            },
            "events": payload["events"],
        }
        return Session.from_json(None, snapshot)  # type: ignore[arg-type]

    async def list(self) -> list[str]:
        rows = await asyncio.to_thread(
            lambda: self._conn.execute("SELECT id FROM sessions").fetchall()
        )
        return [r[0] for r in rows]
