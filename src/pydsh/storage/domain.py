"""``ctx.storage_domain`` — the layer that knows what a record *means*.

Declared domains, schema-validated tables, synchronous reads from memory, one
write chain per domain, and a change event. A consumer depends on this and
never touches a backend.

The rule everything else follows from:

    **durable first, memory second, event third**

A write reaches the medium before the in-memory copy moves. Do it the other way
and a rejected write leaves the reader seeing a value that is stored nowhere —
reads and writes fork, silently, and the next process to open the unit
disagrees with the one that is running. Waiting first costs a round trip and
buys the guarantee that memory is never ahead of disk.

Reads are synchronous because the records are already here. That is what the
layer is *for*: a consumer asking "what is the current goal" should not await a
disk read to find out.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

from plugkit import Service

from .errors import DomainError, StorageError
from .hub import UNIT_NAME

logger = logging.getLogger("pydsh.storage.domain")

#: Emitted after a durable write lands. A notification, never a participant.
DOMAIN_CHANGED = "domain/changed"

#: The backend a domain uses unless it names another.
DEFAULT_BACKEND = "default"

#: Validates one stored value. Return it (or a coerced form); raise to reject.
RecordSchema = Callable[[Any], Any]


def domain_table(validate: Optional[RecordSchema] = None) -> dict:
    """Declare a table. ``validate`` checks every stored record."""
    return {"validate": validate}


@dataclass(frozen=True)
class DomainSpec:
    """A validated domain declaration."""

    name: str
    version: int
    tables: dict = field(default_factory=dict)
    global_: Optional[dict] = None

    def descriptor(self) -> dict:
        """What the backend needs to open the unit."""
        return {
            "name": self.name,
            "version": self.version,
            "tables": list(self.tables),
            "has_global": self.global_ is not None,
        }


def define_domain(
    name: str,
    version: int,
    tables: Optional[dict] = None,
    global_: Optional[dict] = None,
) -> DomainSpec:
    """Declare a domain, failing at import time rather than at first save.

    :param global_: ``{"validate": ..., "initial": ...}`` for a single value
        alongside the tables.
    :raises ValueError: a name that is not a safe identifier, a version that is
        not a non-negative integer, or a global schema that accepts ``None``.
    """
    if not UNIT_NAME.fullmatch(name):
        raise ValueError(
            f"domain name {name!r} must match {UNIT_NAME.pattern} — it becomes a "
            "filename segment and a SQL identifier"
        )
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValueError(
            f"domain {name!r} needs a non-negative integer version, got {version!r}"
        )
    tables = tables or {}
    for table in tables:
        if not UNIT_NAME.fullmatch(table):
            raise ValueError(
                f"domain {name!r} table {table!r} must match {UNIT_NAME.pattern}"
            )
    if global_ is not None:
        validate = global_.get("validate")
        if validate is not None and _accepts_none(validate):
            raise ValueError(
                f"domain {name!r} global schema must not accept null: null is the "
                "medium's 'never written' sentinel, so a nullable global cannot "
                "round-trip — a stored null would silently read back as the initial"
            )
    return DomainSpec(name=name, version=version, tables=dict(tables), global_=global_)


def _accepts_none(validate: RecordSchema) -> bool:
    try:
        validate(None)
    except Exception:  # noqa: BLE001 - rejecting None is what we want
        return False
    return True


def _checked(
    domain: str, table: str, key: str, validate: Optional[RecordSchema], raw: Any
) -> Any:
    """Validate one stored value, saying exactly which slot failed."""
    if validate is None:
        return raw
    try:
        return validate(raw)
    except Exception as exc:  # noqa: BLE001 - any validator, any failure
        slot = "the global value" if not table else f"record {key!r} in table {table!r}"
        raise DomainError(
            "invalid-record",
            f"domain {domain!r}: stored {slot} no longer satisfies its schema",
            detail={"table": table, "key": key},
            cause=exc,
        ) from exc


class Table:
    """A handle on one table: synchronous reads, queued writes."""

    def __init__(self, domain: "Domain", name: str, records: dict) -> None:
        self._domain = domain
        self._name = name
        self._records = records
        self._validate = domain.spec.tables[name].get("validate")

    # -- reads (I6: already in memory, so no await) ------------------------ #
    def get(self, key: str) -> Any:
        self._domain.assert_readable()
        return self._records.get(key)

    def entries(self) -> Iterator[tuple[str, Any]]:
        self._domain.assert_readable()
        return iter(list(self._records.items()))

    def keys(self) -> Iterator[str]:
        self._domain.assert_readable()
        return iter(list(self._records))

    @property
    def size(self) -> int:
        self._domain.assert_readable()
        return len(self._records)

    # -- writes ------------------------------------------------------------ #
    async def put(self, key: str, value: Any) -> None:
        """Store a record: durable, then visible, then announced."""
        checked = _checked(self._domain.name, self._name, key, self._validate, value)

        async def job() -> None:
            await self._domain.unit.put_record(self._name, key, checked)
            self._records[key] = checked
            self._domain.announce(
                {
                    "domain": self._domain.name,
                    "table": self._name,
                    "key": key,
                    "operation": "put",
                    "value": checked,
                }
            )

        await self._domain.enqueue(job)

    async def delete(self, key: str) -> bool:
        """Remove a record; ``False`` when there was nothing to remove."""

        async def job() -> bool:
            # Checked inside the slot, so an earlier queued put is visible here
            # rather than being decided against a stale view.
            if key not in self._records:
                return False
            await self._domain.unit.delete_record(self._name, key)
            del self._records[key]
            self._domain.announce(
                {
                    "domain": self._domain.name,
                    "table": self._name,
                    "key": key,
                    "operation": "deleted",
                }
            )
            return True

        return await self._domain.enqueue(job)

    async def update(self, key: str, change: Callable[[Any], Any]) -> Any:
        """Read-modify-write inside one slot on the chain."""

        async def job() -> Any:
            if key not in self._records:
                raise DomainError(
                    "missing-key",
                    f"domain {self._domain.name!r} table {self._name!r} has no "
                    f"record {key!r} to update",
                    detail={"table": self._name, "key": key},
                )
            nxt = _checked(
                self._domain.name,
                self._name,
                key,
                self._validate,
                change(self._records[key]),
            )
            await self._domain.unit.put_record(self._name, key, nxt)
            self._records[key] = nxt
            self._domain.announce(
                {
                    "domain": self._domain.name,
                    "table": self._name,
                    "key": key,
                    "operation": "put",
                    "value": nxt,
                }
            )
            return nxt

        return await self._domain.enqueue(job)


class GlobalHandle:
    """The domain's single value, if it declared one."""

    def __init__(self, domain: "Domain") -> None:
        self._domain = domain

    def get(self) -> Any:
        self._domain.assert_readable()
        return self._domain.global_value

    async def set(self, value: Any) -> None:
        spec = self._domain.spec.global_ or {}
        checked = _checked(self._domain.name, "", "", spec.get("validate"), value)

        async def job() -> None:
            await self._domain.unit.set_global(checked)
            self._domain.global_value = checked
            self._domain.announce(
                {
                    "domain": self._domain.name,
                    "table": "",
                    "key": "",
                    "operation": "put",
                    "value": checked,
                }
            )

        await self._domain.enqueue(job)


class Domain:
    """One opened domain: memory, one write chain, and change events."""

    def __init__(
        self,
        ctx: Any,
        spec: DomainSpec,
        unit: Any,
        records: dict,
        global_value: Any,
        on_closed: Callable[[], None],
    ) -> None:
        self._ctx = ctx
        self.spec = spec
        self.name = spec.name
        self.unit = unit
        self.global_value = global_value
        self._on_closed = on_closed
        # One lock per domain: this is the write chain (I2). Two callers
        # writing one key land in a defined order, and the medium's order
        # matches the order the change events go out in.
        self._lock = asyncio.Lock()
        self._closing = False
        self._closed = False
        self._tables = {
            name: Table(self, name, records.get(name, {})) for name in spec.tables
        }

    def table(self, name: str) -> Table:
        table = self._tables.get(name)
        if table is None:
            declared = ", ".join(sorted(self._tables)) or "none"
            raise DomainError(
                "missing-key",
                f"domain {self.name!r} declares no table {name!r} (declared: {declared})",
            )
        return table

    @property
    def global_(self) -> GlobalHandle:
        if self.spec.global_ is None:
            raise DomainError(
                "missing-key", f"domain {self.name!r} declares no global value"
            )
        return GlobalHandle(self)

    # -- the write chain --------------------------------------------------- #
    async def enqueue(self, job: Callable[[], Any]) -> Any:
        """Run one write in this domain's slot.

        Takes a callable rather than a coroutine so that refusing a write on a
        closing domain does not leave an un-awaited coroutine behind — the
        reference builds the coroutine eagerly and reports that case as a
        `RuntimeWarning` at collection time, far from the cause.
        """
        if self._closing:
            raise DomainError("closed", f"domain {self.name!r} is closed")
        async with self._lock:
            return await job()

    def assert_readable(self) -> None:
        if self._closed:
            raise DomainError("closed", f"domain {self.name!r} is closed")

    def announce(self, change: dict) -> None:
        """Publish a change. Contained: the commit point has already passed."""
        try:
            self._ctx.emit(DOMAIN_CHANGED, change)
        except Exception as exc:  # noqa: BLE001 - a listener is not a participant
            logger.warning(
                "domain %r: a %s listener failed: %s",
                self.name,
                DOMAIN_CHANGED,
                exc,
                exc_info=exc,
            )

    async def close(self) -> None:
        """Refuse new writes, drain the queued ones, release the unit. Idempotent."""
        if self._closed:
            return
        self._closing = True
        async with self._lock:  # drain: wait for writes already in flight
            pass
        await self.unit.close()
        self._closed = True
        self._on_closed()


class StorageDomain(Service):
    """Provides ``ctx.storage_domain`` — opening declared domains on backends."""

    provide = "storage_domain"
    inject = ["storage"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        #: Per-domain backend overrides; everything else takes the default.
        self._routes: dict[str, str] = dict(config.get("routes") or {})
        self._default = config.get("backend", DEFAULT_BACKEND)
        self._open: dict[str, Domain] = {}
        unmount = ctx.storage.mount("domain", self)
        ctx.effect(lambda: lambda: (unmount(), self._abandon()))

    def _abandon(self) -> None:
        """Unmounted: forget the open domains.

        Closing them properly needs an await and teardown is synchronous. The
        units are file handles and shared connections owned by their backends,
        which are torn down by their own plugins.
        """
        self._open.clear()

    def route(self, spec: DomainSpec) -> str:
        """Which backend serves this domain."""
        return self._routes.get(spec.name, self._default)

    async def open(self, spec: DomainSpec, backend: Optional[str] = None) -> Domain:
        """Open a declared domain, validating everything already stored in it."""
        if spec.name in self._open:
            raise DomainError(
                "duplicate-domain",
                f"domain {spec.name!r} is already open — two runtimes over one "
                "medium would each believe their memory is authoritative",
            )

        target = backend or self.route(spec)
        registered = self.ctx.storage.backend.get(target)
        facet = getattr(registered, "kv", None)
        if facet is None:
            raise StorageError(
                "no-facet",
                f"storage backend {target!r} cannot serve key-value units, so "
                f"domain {spec.name!r} cannot be opened on it",
            )

        unit = await facet.open(spec.descriptor())
        stored = await unit.load_all()

        records: dict[str, dict] = {}
        for table, declared in spec.tables.items():
            validate = declared.get("validate")
            records[table] = {
                key: _checked(spec.name, table, key, validate, raw)
                for key, raw in (stored["tables"].get(table) or {}).items()
            }

        global_value = None
        if spec.global_ is not None:
            raw = stored.get("global")
            global_value = (
                spec.global_.get("initial")
                if raw is None
                else _checked(spec.name, "", "", spec.global_.get("validate"), raw)
            )

        domain = Domain(
            self.ctx,
            spec,
            unit,
            records,
            global_value,
            lambda: self._open.pop(spec.name, None),
        )
        self._open[spec.name] = domain
        return domain

    def is_open(self, name: str) -> bool:
        return name in self._open

    async def close_all(self) -> None:
        """Close every open domain — for shutdown and for tests."""
        for domain in list(self._open.values()):
            await domain.close()


__all__ = [
    "StorageDomain",
    "Domain",
    "Table",
    "GlobalHandle",
    "DomainSpec",
    "define_domain",
    "domain_table",
    "DOMAIN_CHANGED",
    "DEFAULT_BACKEND",
]
