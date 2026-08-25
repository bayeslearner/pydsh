"""``ctx.settings`` — configuration that can change while the process runs.

A section is registered under a namespace with a validator and a starting
value. Anything that wants to be tunable at runtime reads through it at the
moment of use rather than capturing a value at construction — which is the
whole difference between *configurable* and *configured once*.

The agent loop's parallel-tool limit is the motivating case: spec 03 had to
take it as a constructor argument because there was nothing to read it from, so
changing it meant building a new agent.

Validation happens **before** the write lands, so a rejected value leaves the
old one standing rather than a half-applied one.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from plugkit import Service

logger = logging.getLogger("pydsh.settings")

#: Validates a section's value. Return it (or a coerced form); raise to reject.
Validator = Callable[[Any], Any]


class UnknownNamespaceError(KeyError):
    """A namespace nobody registered."""


class SettingsScope:
    """One readable, writable section."""

    def __init__(
        self,
        namespace: str,
        validate: Optional[Validator] = None,
        base: Any = None,
    ) -> None:
        self.namespace = namespace
        self._validate = validate
        self._value = validate(base) if (validate and base is not None) else base
        # Each entry is its own object rather than the bare callback: the same
        # callable may be registered twice, and identity on the callback cannot
        # tell those two registrations apart — so one handle's unwatch would
        # silently consume the other's.
        self._watchers: list[list] = []

    def get(self) -> Any:
        """The current value."""
        return self._value

    def set(self, value: Any) -> None:
        """Validate, store, then notify.

        In that order: a validator that raises leaves the old value in place,
        so a rejected write cannot half-apply.
        """
        if self._validate is not None:
            value = self._validate(value)
        self._value = value
        for entry in list(self._watchers):
            try:
                entry[0]()
            except Exception as exc:  # noqa: BLE001 - one watcher is not the rest
                logger.warning(
                    "settings %r: a watcher failed: %s", self.namespace, exc, exc_info=exc
                )

    def watch(self, callback: Callable[[], None]) -> Callable[[], bool]:
        """Register a change watcher; returns an unwatch for *this* registration."""
        entry = [callback]
        self._watchers.append(entry)

        def unwatch() -> bool:
            for index, candidate in enumerate(self._watchers):
                if candidate is entry:
                    self._watchers.pop(index)
                    return True
            return False

        return unwatch


class Settings(Service):
    """Provides ``ctx.settings``."""

    provide = "settings"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._sections: dict[str, SettingsScope] = {}

    def register(
        self,
        namespace: str,
        validate: Optional[Validator] = None,
        base: Any = None,
    ) -> SettingsScope:
        """Register a section and return its scope."""
        scope = SettingsScope(namespace, validate, base)
        self._sections[namespace] = scope
        return scope

    def scope(self, namespace: str) -> SettingsScope:
        section = self._sections.get(namespace)
        if section is None:
            known = ", ".join(sorted(self._sections)) or "none"
            raise UnknownNamespaceError(
                f"settings namespace {namespace!r} is not registered (registered: {known})"
            )
        return section

    def get(self, namespace: str) -> Any:
        return self.scope(namespace).get()

    def set(self, namespace: str, value: Any) -> None:
        self.scope(namespace).set(value)

    def has(self, namespace: str) -> bool:
        return namespace in self._sections

    def namespaces(self) -> list[str]:
        return sorted(self._sections)


__all__ = ["Settings", "SettingsScope", "UnknownNamespaceError", "Validator"]
