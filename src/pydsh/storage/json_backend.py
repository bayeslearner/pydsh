"""One JSON file per unit — the simplest medium that is still safe.

A unit is a single file holding its version, its tables, and its global slot.
The whole file is rewritten through an atomic replace on every durable write:
slow for a large unit, but correct, dependency-free, and easy to inspect when
something has gone wrong. A deployment that has outgrown it registers the
SQLite backend instead — that swap is the reason the hub is a named registry.

The in-memory copy here is *not* the same thing as the domain layer's. This one
exists so a rewrite can serialize the whole unit; the domain layer's is the
read path. They are kept identical by the domain's durable-before-visible rule.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from plugkit import Service

from .atomic import write_file_atomic
from .errors import StorageError

#: The on-disk shape's version. Distinct from a *unit's* declared version:
#: this is the envelope, that is the payload.
MEDIUM_FORMAT = 1


def _read_unit_file(path: Path, name: str) -> Optional[dict]:
    """Read a unit file: ``None`` if absent, raise if unreadable or wrong shape."""
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        raise StorageError(
            "malformed-medium",
            f"storage-json: cannot read unit {name!r} at {str(path)!r}",
            cause=exc,
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("tables"), dict):
        raise StorageError(
            "malformed-medium",
            f"storage-json: unit file for {name!r} at {str(path)!r} has the wrong "
            "shape (expected an object with a 'tables' object)",
        )
    return data


class JsonKvUnit:
    """One opened JSON unit."""

    def __init__(self, path: Path, descriptor: dict) -> None:
        self._path = path
        self._name = descriptor["name"]
        self._version = descriptor["version"]
        self._closed = False

        stored = _read_unit_file(path, self._name)
        if stored is None:
            self._tables: dict[str, dict[str, Any]] = {
                table: {} for table in descriptor["tables"]
            }
            self._global: Any = None
            self._persist()
            return

        if stored.get("version") != self._version:
            raise StorageError(
                "version-mismatch",
                f"storage-json: unit {self._name!r} on disk is version "
                f"{stored.get('version')!r}, expected {self._version!r}",
            )
        self._tables = {
            table: dict(records) for table, records in stored["tables"].items()
        }
        # A table declared since the unit was written starts empty rather than
        # missing — the descriptor is the current shape, the file is history.
        for table in descriptor["tables"]:
            self._tables.setdefault(table, {})
        self._global = stored.get("global")

    def _assert_open(self) -> None:
        if self._closed:
            raise StorageError(
                "closed", f"storage-json: unit {self._name!r} is closed"
            )

    def _persist(self) -> None:
        write_file_atomic(
            self._path,
            json.dumps(
                {
                    "format": MEDIUM_FORMAT,
                    "name": self._name,
                    "version": self._version,
                    "tables": self._tables,
                    "global": self._global,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )

    async def load_all(self) -> dict:
        self._assert_open()
        return {
            "tables": {table: dict(rows) for table, rows in self._tables.items()},
            "global": self._global,
        }

    async def put_record(self, table: str, key: str, value: Any) -> None:
        self._assert_open()
        self._tables.setdefault(table, {})[key] = value
        self._persist()

    async def delete_record(self, table: str, key: str) -> None:
        self._assert_open()
        self._tables.get(table, {}).pop(key, None)
        self._persist()

    async def set_global(self, value: Any) -> None:
        self._assert_open()
        self._global = value
        self._persist()

    async def close(self) -> None:
        self._closed = True


class JsonKvFacet:
    """Opens JSON units under one root directory."""

    def __init__(self, root: str) -> None:
        self._root = Path(root)

    async def open(self, descriptor: dict) -> JsonKvUnit:
        # The name is constrained to a safe identifier by `define_domain`, so
        # it cannot escape the root as a path segment.
        return JsonKvUnit(self._root / f"{descriptor['name']}.json", descriptor)


class JsonBackend:
    """A JSON-file backend. A plain object — register it on the hub yourself.

    Plain rather than a plugkit service because backends are **plural**: a
    deployment may register several, and a service class can only provide its
    name once. Registering by hand is also what makes the name a deployment
    decision rather than something a plugin chose::

        root.storage.backend.register("files", JsonBackend("/var/lib/app"))
    """

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.kv = JsonKvFacet(root)

    async def close(self) -> None:
        """Nothing to release: each unit opens and closes its file per write."""


class JsonStorage(Service):
    """The one-backend convenience: build a :class:`JsonBackend` and register it.

    For a deployment with several backends, register :class:`JsonBackend`
    instances directly instead.
    """

    provide = "json_storage"
    inject = ["storage"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        root = config.get("root") or os.path.join(os.getcwd(), ".pydsh", "storage")
        self.backend = JsonBackend(root)
        self.root = self.backend.root
        self.kv = self.backend.kv
        dispose = ctx.storage.backend.register(config.get("name", "default"), self.backend)
        ctx.effect(lambda: dispose)


__all__ = ["JsonBackend", "JsonStorage", "JsonKvFacet", "JsonKvUnit", "MEDIUM_FORMAT"]
