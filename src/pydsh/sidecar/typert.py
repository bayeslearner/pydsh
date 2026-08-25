"""``ctx.typert`` — the declarative remote-call protocol, by reflection.

A class marks methods remotable, the registry collects them, and a client
invokes by scope and name. The reference runs a TypeScript compiler to generate
bindings; that generator exists because TypeScript cannot read its own
decorators at runtime. Python can, so the code generator becomes a scan.

Exposure is **opt-in**. Nothing is remotable by being public, so adding a helper
method to a service does not quietly widen the remote API — the failure mode of
expose-by-default.

An invocation never raises at the caller. The caller is on the other side of a
transport that cannot carry a Python traceback, so every outcome — including a
handler blowing up — comes back as a :class:`RemoteResult`.
"""

from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from plugkit import Service

#: Written onto a function by :func:`remote`.
REMOTE_ATTR = "_typert_remote"
#: The wire name a remotable method answers to.
WIRE_ATTR = "_typert_wire"
#: Written onto a class by :func:`remote_scope`.
SCOPE_ATTR = "_typert_scope"


def remote(method: Optional[str] = None) -> Callable:
    """Mark a method remotable, optionally under a different wire name."""

    def decorator(fn: Callable) -> Callable:
        wire = method or fn.__name__
        if not wire:
            raise ValueError("typert: a remotable method needs a non-empty wire name")
        setattr(fn, REMOTE_ATTR, True)
        setattr(fn, WIRE_ATTR, wire)
        return fn

    return decorator


def remote_scope(name: Optional[str] = None) -> Callable:
    """Mark a class as a remote scope, optionally under a different name."""

    def decorator(cls: type) -> type:
        scope = name or cls.__name__
        if not scope:
            raise ValueError("typert: a remote scope needs a non-empty name")
        # Set on the class itself rather than inherited: a subclass that does
        # not re-decorate would otherwise register under its parent's name and
        # silently take over that scope.
        setattr(cls, SCOPE_ATTR, scope)
        return cls

    return decorator


def scope_name_of(obj: Any) -> Optional[str]:
    """The declared scope of an object's class, or ``None``."""
    return type(obj).__dict__.get(SCOPE_ATTR) or getattr(type(obj), SCOPE_ATTR, None)


@dataclass(frozen=True)
class RemoteFailure:
    """A stable failure: a code to route on, a message to read."""

    code: str
    message: str


@dataclass(frozen=True)
class RemoteResult:
    """What an invocation returns — always this, never an exception."""

    ok: bool
    value: Any = None
    error: Optional[RemoteFailure] = None

    @staticmethod
    def success(value: Any) -> "RemoteResult":
        return RemoteResult(ok=True, value=value)

    @staticmethod
    def failure(code: str, message: str) -> "RemoteResult":
        return RemoteResult(ok=False, error=RemoteFailure(code=code, message=message))


@dataclass
class InvocationDescriptor:
    """One remote call, fully described.

    ``id`` is the caller's handle on this invocation — it travels with the
    result over a transport, which is why the descriptor carries one at all.
    """

    service: str
    method: str
    args: dict = field(default_factory=dict)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex


def _remotable_methods(obj: Any) -> dict:
    """The object's remotable methods, by wire name.

    Scanned off the **class**, not the instance. ``dir(obj)`` plus ``getattr``
    would run every property getter on the object just to find out whether it
    is remotable — a scan with side effects, and one that raises if any
    property does.
    """
    methods: dict[str, Callable] = {}
    for name, member in inspect.getmembers(type(obj), callable):
        if not getattr(member, REMOTE_ATTR, False):
            continue
        wire = getattr(member, WIRE_ATTR, None) or name
        if wire in methods:
            raise ValueError(
                f"typert: {type(obj).__name__} exposes two methods under the wire "
                f"name {wire!r} — one would shadow the other"
            )
        methods[wire] = getattr(obj, name)
    return methods


class TypertRegistry(Service):
    """Provides ``ctx.typert`` — the endpoint registry and its dispatcher."""

    provide = "typert"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._scopes: dict[str, dict] = {}

    # -- registration ------------------------------------------------------ #
    def register(self, obj: Any, scope: Optional[str] = None) -> Callable[[], None]:
        """Scan an object for remotable methods and expose them.

        :param scope: the wire scope; defaults to the ``@remote_scope`` name.
        :raises ValueError: no scope name, or no remotable methods (R3.4) —
            registering nothing looks exactly like registering something.
        """
        resolved = scope or scope_name_of(obj)
        if not resolved:
            raise ValueError(
                f"typert: {type(obj).__name__} has no scope name — decorate the "
                "class with @remote_scope or pass scope="
            )
        methods = _remotable_methods(obj)
        if not methods:
            raise ValueError(
                f"typert: {type(obj).__name__} has no @remote methods, so "
                f"registering it under {resolved!r} would expose nothing"
            )
        entry = {"object": obj, "methods": methods}
        self._scopes[resolved] = entry

        def dispose() -> None:
            # Identity-guarded: a later registration under the same scope owns
            # it now, and this disposer must not remove someone else's entry.
            if self._scopes.get(resolved) is entry:
                del self._scopes[resolved]

        return dispose

    def has_scope(self, scope: str) -> bool:
        return scope in self._scopes

    def list(self) -> list[dict]:
        """Every registered endpoint (R3.7)."""
        return [
            {"service": scope, "methods": sorted(entry["methods"])}
            for scope, entry in sorted(self._scopes.items())
        ]

    # -- dispatch ---------------------------------------------------------- #
    async def invoke(self, descriptor: InvocationDescriptor) -> RemoteResult:
        """Dispatch one call. Never raises (R3.5)."""
        entry = self._scopes.get(descriptor.service)
        if entry is None:
            available = ", ".join(sorted(self._scopes)) or "none"
            return RemoteResult.failure(
                "SCOPE_NOT_FOUND",
                f"no remote scope {descriptor.service!r}; registered: {available}",
            )
        fn = entry["methods"].get(descriptor.method)
        if fn is None:
            available = ", ".join(sorted(entry["methods"]))
            return RemoteResult.failure(
                "METHOD_NOT_FOUND",
                f"scope {descriptor.service!r} has no method "
                f"{descriptor.method!r}; available: {available}",
            )
        # Bind first, call second. A caller's bad arguments and a handler's own
        # TypeError are otherwise indistinguishable from the outside, and
        # reporting the first as the second sends a client looking for a server
        # fault that never happened.
        try:
            inspect.signature(fn).bind(**descriptor.args)
        except TypeError as exc:
            return RemoteResult.failure(
                "BAD_ARGUMENTS", f"{descriptor.service}.{descriptor.method}: {exc}"
            )
        except (ValueError, AttributeError):
            pass  # no introspectable signature; let the call itself decide

        try:
            result = fn(**descriptor.args)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 - a remote failure is a result
            return RemoteResult.failure("FAILED", f"{type(exc).__name__}: {exc}")
        return RemoteResult.success(result)

    def client_for(self, scope: str) -> Any:
        """A proxy whose attribute calls become invocations of ``scope``."""
        registry = self

        class RemoteProxy:
            def __getattr__(self, name: str) -> Callable:
                if name.startswith("_"):
                    raise AttributeError(name)

                async def call(**args: Any) -> RemoteResult:
                    return await registry.invoke(
                        InvocationDescriptor(service=scope, method=name, args=args)
                    )

                return call

        return RemoteProxy()


__all__ = [
    "remote",
    "remote_scope",
    "scope_name_of",
    "TypertRegistry",
    "InvocationDescriptor",
    "RemoteResult",
    "RemoteFailure",
    "REMOTE_ATTR",
    "WIRE_ATTR",
    "SCOPE_ATTR",
]
