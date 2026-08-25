"""``ctx.storage`` — the phone book, not the database.

The hub does **no I/O at all**. It holds two tables:

- **backends**, by name. Plural on purpose: which backend serves which consumer
  is the *consumer's* configuration, never a global choice made here. One
  deployment can keep settings in a JSON file and attachments in SQLite.
- **forms**, by name. A form is a way of *using* backends — ``domain`` is the
  only one this port ships — and mounting one is what makes it reachable.

Registering does not transfer ownership. A disposer removes the name; closing
the backend stays with the plugin that opened it, because that plugin knows
when its medium is really finished with.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional, Protocol

from plugkit import Service

from .errors import StorageError

#: Legal unit and table names: safe as a filename segment and as an unescaped
#: SQL identifier, which is exactly the set of places a name ends up.
UNIT_NAME = re.compile(r"[a-z][a-z0-9_]*")


class KvUnit(Protocol):
    """One opened key-value container.

    Values are opaque JSON at this layer: no schema, no events, no meaning.
    A unit does **not** serialize concurrent writers — ordering is the caller's
    job, and the domain form is what provides it. A unit guarantees only that
    one call is atomic on the medium and durable once it returns.
    """

    async def load_all(self) -> dict: ...
    async def put_record(self, table: str, key: str, value: Any) -> None: ...
    async def delete_record(self, table: str, key: str) -> None: ...
    async def set_global(self, value: Any) -> None: ...
    async def close(self) -> None: ...


class KvFacet(Protocol):
    """A backend's key-value capability."""

    async def open(self, descriptor: dict) -> KvUnit: ...


class StorageBackend(Protocol):
    """A registered backend: owns one medium, shares its lifetime.

    ``kv`` is optional. A backend that cannot serve a shape omits the facet
    rather than stubbing it, so resolution fails loudly at open instead of
    quietly at the first write.
    """

    kv: Optional[KvFacet]

    async def close(self) -> None: ...


class BackendRegistry:
    """Named backends. Registration is an effect; the disposer removes a name."""

    def __init__(self) -> None:
        self._backends: dict[str, Any] = {}

    def register(self, name: str, backend: Any) -> Callable[[], None]:
        """Register a backend under a name; returns its disposer."""
        if name in self._backends:
            raise StorageError(
                "duplicate-backend", f"storage backend {name!r} is already registered"
            )
        self._backends[name] = backend

        def dispose() -> None:
            # Only this registration's contribution: after a dispose and a
            # re-register, the stale disposer must not remove its successor.
            if self._backends.get(name) is backend:
                self._backends.pop(name, None)

        return dispose

    def get(self, name: str) -> Any:
        backend = self._backends.get(name)
        if backend is None:
            registered = ", ".join(sorted(self._backends)) or "none"
            raise StorageError(
                "backend-not-found",
                f"storage backend {name!r} is not registered (registered: {registered})",
            )
        return backend

    def names(self) -> list[str]:
        return sorted(self._backends)


class Storage(Service):
    """Provides ``ctx.storage``."""

    provide = "storage"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self.backend = BackendRegistry()
        self._forms: dict[str, Any] = {}

    def mount(self, form: str, facility: Any) -> Callable[[], None]:
        """Mount a data form; returns its unmount."""
        if form in self._forms:
            raise StorageError(
                "duplicate-mount", f"storage form {form!r} is already mounted"
            )
        self._forms[form] = facility

        def unmount() -> None:
            if self._forms.get(form) is facility:
                self._forms.pop(form, None)

        return unmount

    def form(self, form: str) -> Any:
        """A mounted form, or a clear failure naming what is mounted."""
        facility = self._forms.get(form)
        if facility is None:
            mounted = ", ".join(sorted(self._forms)) or "none"
            raise StorageError(
                "form-not-mounted",
                f"storage form {form!r} is not mounted (mounted: {mounted})",
            )
        return facility

    def forms(self) -> list[str]:
        return sorted(self._forms)

    @property
    def domain(self) -> Any:
        """The domain form — available once its facility is mounted."""
        return self.form("domain")


__all__ = [
    "Storage",
    "BackendRegistry",
    "KvUnit",
    "KvFacet",
    "StorageBackend",
    "UNIT_NAME",
]
