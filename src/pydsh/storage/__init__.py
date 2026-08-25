"""Storage — a hub, two media, and one place that owns what a record means.

Three layers with a sharp split. The hub does no I/O; a backend owns one medium
and knows nothing about meaning; the domain form owns the meaning::

    await root.plugin(Storage)                              # ctx.storage
    await root.plugin(JsonStorage, {"root": "/var/lib/app"})
    await root.plugin(StorageDomain)                        # ctx.storage_domain

    GOALS = define_domain("goals", version=1,
                          tables={"entries": domain_table(validate_goal)})

    goals = await root.storage_domain.open(GOALS)
    await goals.table("entries").put("g1", {"text": "ship the port"})
    goals.table("entries").get("g1")     # synchronous — already in memory

Writes go durable first, memory second, event third, so the in-memory read path
is never ahead of what is on disk.
"""

from .atomic import write_file_atomic
from .domain import (
    DEFAULT_BACKEND,
    DOMAIN_CHANGED,
    Domain,
    DomainSpec,
    GlobalHandle,
    StorageDomain,
    Table,
    define_domain,
    domain_table,
)
from .errors import STORAGE_ERROR_CODES, DomainError, StorageError
from .hub import (
    UNIT_NAME,
    BackendRegistry,
    KvFacet,
    KvUnit,
    Storage,
    StorageBackend,
)
from .json_backend import JsonBackend, JsonKvFacet, JsonKvUnit, JsonStorage
from .sqlite_backend import SqliteBackend, SqliteKvFacet, SqliteKvUnit, SqliteStorage

__all__ = [
    # the hub
    "Storage",
    "BackendRegistry",
    "KvUnit",
    "KvFacet",
    "StorageBackend",
    "UNIT_NAME",
    # media
    "JsonBackend",
    "JsonStorage",
    "JsonKvFacet",
    "JsonKvUnit",
    "SqliteBackend",
    "SqliteStorage",
    "SqliteKvFacet",
    "SqliteKvUnit",
    # the domain form
    "StorageDomain",
    "Domain",
    "Table",
    "GlobalHandle",
    "DomainSpec",
    "define_domain",
    "domain_table",
    "DOMAIN_CHANGED",
    "DEFAULT_BACKEND",
    # errors + utils
    "StorageError",
    "DomainError",
    "STORAGE_ERROR_CODES",
    "write_file_atomic",
]
