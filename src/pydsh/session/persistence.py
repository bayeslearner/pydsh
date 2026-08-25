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
from .session import Session, SessionEvent, SessionError, SessionHeader

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


def _encode_surface_op(surface_op: Any) -> Optional[str]:
    """The surface operation as text SQLite can hold.

    ``"append"`` and ``None`` stay as they are; a replacement is a mapping and
    has to be encoded. Without this a compacted session fails to persist at the
    moment it is compacted, which is the worst possible time.
    """
    if surface_op is None or isinstance(surface_op, str):
        return surface_op
    return json.dumps(surface_op)


def _decode_surface_op(stored: Any) -> Any:
    """Inverse of :func:`_encode_surface_op`."""
    if stored is None or not isinstance(stored, str):
        return stored
    if not stored.startswith("{"):
        return stored
    try:
        return json.loads(stored)
    except ValueError:
        return stored


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

    async def read_from(self, id: str, from_seq: int) -> Optional[dict]:  # pragma: no cover - ABC
        """The header plus the events at or after ``from_seq``.

        The cold-read path: a consumer that already holds folded state needs
        only the tail since that state's watermark, not the whole log. Returns
        ``None`` when the session was never persisted, so "no such session" is
        distinguishable from "a session with no events left to read".
        """
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

    def _write(self, session: Session) -> int:
        payload = session.to_json()
        header = payload["header"]
        events = payload["events"]
        # Captured from the snapshot being written, for the same reason
        # `_write_unsaved` does: `session.seq` may have moved on since.
        written = events[-1]["seq"] if events else 0
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
                        _encode_surface_op(e.get("surface_op")),
                        json.dumps(e.get("source_event_seqs", [])),
                    ),
                )
        return written

    def _write_unsaved(self, session: Session) -> int:
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
        # The watermark is what this call actually wrote, and it is captured
        # HERE rather than read back from `session.seq` afterwards. The session
        # is live: events can be appended while this runs, and recording the
        # session's current tail would mark those as persisted without ever
        # writing them — the next incremental flush would then skip them, and
        # they would be lost with no error anywhere.
        written = fresh[-1].seq if fresh else from_seq
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
                        _encode_surface_op(e.surface_op),
                        json.dumps(list(e.source_event_seqs)),
                    ),
                )
        return written

    def _read_header(self, id: str) -> Optional[SessionHeader]:
        """The stored header alone — the identity witness a cold read checks."""
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
        return SessionHeader(
            version=version,
            id=id,
            created_at=created_at,
            cwd=cwd,
            request=json.loads(request) if request else None,
        )

    def _read_events_from(self, id: str, from_seq: int) -> list[SessionEvent]:
        """The events at or after ``from_seq``, in order."""
        rows = self._conn.execute(
            "SELECT type, seq, time, data, surface_op, source_event_seqs"
            " FROM events WHERE session_id = ? AND seq >= ? ORDER BY seq",
            (id, from_seq),
        ).fetchall()
        return [
            SessionEvent(
                type=r[0],
                seq=r[1],
                time=r[2],
                data=json.loads(r[3]),
                surface_op=_decode_surface_op(r[4]),
                source_event_seqs=tuple(json.loads(r[5]) if r[5] else []),
            )
            for r in rows
        ]

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
                    "surface_op": _decode_surface_op(r[4]),
                    "source_event_seqs": json.loads(r[5]) if r[5] else [],
                }
                for r in rows
            ],
        }

    # -- SessionPersistence -------------------------------------------------

    async def create(self, session: Session) -> None:
        async with self._lock:
            written = await asyncio.to_thread(self._write, session)
            self._persisted_seq[id(session)] = written

    async def flush(self, session: Session) -> None:
        """Write everything appended since the last checkpoint.

        The watermark comes from what the write actually covered, never from
        the session's tail at the moment the write finished — the session is
        live, and anything appended during the write would otherwise be marked
        persisted without being written.
        """
        async with self._lock:
            written = await asyncio.to_thread(self._write_unsaved, session)
            self._persisted_seq[id(session)] = written

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

    async def read_from(self, id: str, from_seq: int) -> Optional[dict]:
        header = await asyncio.to_thread(self._read_header, id)
        if header is None:
            return None
        events = await asyncio.to_thread(self._read_events_from, id, from_seq)
        return {"meta": header, "events": events}

    async def list(self) -> list[str]:
        rows = await asyncio.to_thread(
            lambda: self._conn.execute("SELECT id FROM sessions").fetchall()
        )
        return [r[0] for r in rows]
