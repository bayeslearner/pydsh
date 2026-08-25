"""The same unit contract over SQLite — one row per record, not one file per unit.

The JSON backend rewrites a whole unit on every write. That is fine for a
handful of settings and wrong for a table with ten thousand rows. This backend
serves the *same* contract so a deployment swaps between them by configuration,
which is only true if they behave alike — the conformance suite runs every test
against both for exactly that reason.

``sqlite3`` blocks, so every call is stepped off the event loop. One connection
per unit, guarded by a lock, because a connection is not safe to use from two
coroutines at once even when each call is itself atomic.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from plugkit import Service

from .errors import StorageError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS storage_units (
    unit    TEXT PRIMARY KEY,
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS storage_records (
    unit  TEXT NOT NULL,
    tbl   TEXT NOT NULL,
    key   TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (unit, tbl, key)
);
CREATE TABLE IF NOT EXISTS storage_global (
    unit  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SqliteKvUnit:
    """One opened unit, backed by rows in a shared database."""

    def __init__(self, connection: sqlite3.Connection, lock: asyncio.Lock, descriptor: dict) -> None:
        self._conn = connection
        self._lock = lock
        self._name = descriptor["name"]
        self._version = descriptor["version"]
        self._tables = list(descriptor["tables"])
        self._closed = False

        row = self._conn.execute(
            "SELECT version FROM storage_units WHERE unit = ?", (self._name,)
        ).fetchone()
        if row is None:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO storage_units (unit, version) VALUES (?, ?)",
                    (self._name, self._version),
                )
        elif row[0] != self._version:
            raise StorageError(
                "version-mismatch",
                f"storage-sqlite: unit {self._name!r} on disk is version {row[0]!r}, "
                f"expected {self._version!r}",
            )

    def _assert_open(self) -> None:
        if self._closed:
            raise StorageError(
                "closed", f"storage-sqlite: unit {self._name!r} is closed"
            )

    async def _run(self, work) -> Any:
        """Run one blocking database call off the loop, one at a time."""
        async with self._lock:
            return await asyncio.to_thread(work)

    async def load_all(self) -> dict:
        self._assert_open()

        def work() -> dict:
            tables: dict[str, dict[str, Any]] = {table: {} for table in self._tables}
            rows = self._conn.execute(
                "SELECT tbl, key, value FROM storage_records WHERE unit = ?",
                (self._name,),
            ).fetchall()
            for table, key, value in rows:
                tables.setdefault(table, {})[key] = json.loads(value)
            row = self._conn.execute(
                "SELECT value FROM storage_global WHERE unit = ?", (self._name,)
            ).fetchone()
            return {
                "tables": tables,
                "global": json.loads(row[0]) if row is not None else None,
            }

        return await self._run(work)

    async def put_record(self, table: str, key: str, value: Any) -> None:
        self._assert_open()
        encoded = json.dumps(value, ensure_ascii=False)

        def work() -> None:
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO storage_records (unit, tbl, key, value)"
                    " VALUES (?, ?, ?, ?)",
                    (self._name, table, key, encoded),
                )

        await self._run(work)

    async def delete_record(self, table: str, key: str) -> None:
        self._assert_open()

        def work() -> None:
            with self._conn:
                self._conn.execute(
                    "DELETE FROM storage_records WHERE unit = ? AND tbl = ? AND key = ?",
                    (self._name, table, key),
                )

        await self._run(work)

    async def set_global(self, value: Any) -> None:
        self._assert_open()
        encoded = json.dumps(value, ensure_ascii=False)

        def work() -> None:
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO storage_global (unit, value) VALUES (?, ?)",
                    (self._name, encoded),
                )

        await self._run(work)

    async def close(self) -> None:
        # The connection belongs to the backend and may serve other units.
        self._closed = True


class SqliteKvFacet:
    """Opens units against one database file."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._lock = asyncio.Lock()

    async def open(self, descriptor: dict) -> SqliteKvUnit:
        return SqliteKvUnit(self._conn, self._lock, descriptor)


class SqliteBackend:
    """A SQLite backend. A plain object — register it on the hub yourself.

    Plain rather than a plugkit service because backends are **plural**: a
    deployment may register several, and a service class can only provide its
    name once.
    """

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self.kv = SqliteKvFacet(self._conn)

    async def close(self) -> None:
        self._conn.close()

    def close_sync(self) -> None:
        """Release the connection from a synchronous teardown."""
        self._conn.close()


class SqliteStorage(Service):
    """The one-backend convenience: build a :class:`SqliteBackend`, register it.

    For a deployment with several backends, register :class:`SqliteBackend`
    instances directly instead.
    """

    provide = "sqlite_storage"
    inject = ["storage"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        path = config.get("path") or os.path.join(os.getcwd(), ".pydsh", "storage.db")
        self.backend = SqliteBackend(path)
        self.path = path
        self.kv = self.backend.kv
        dispose = ctx.storage.backend.register(config.get("name", "default"), self.backend)
        # `ctx.effect` runs its argument *now* and keeps the RETURN VALUE as the
        # teardown, so the body must return the closure rather than be it.
        ctx.effect(lambda: lambda: self._shutdown(dispose))

    def _shutdown(self, dispose) -> None:
        dispose()
        self.backend.close_sync()


__all__ = ["SqliteBackend", "SqliteStorage", "SqliteKvFacet", "SqliteKvUnit"]
