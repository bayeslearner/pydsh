"""``ctx.invariants`` — making a violated assumption loud rather than mysterious.

A plugin registers a named predicate about state that should always hold. A
sweep runs them all and reports which failed, with the description.

The point is the *description*. "Invariant 3 failed" tells an operator nothing;
"every open session has a persistence backend attached" tells them what to look
at. An invariant that cannot say what it checked is worse than none, because it
reads like a guarantee while providing no diagnosis.

A predicate that raises is reported as failed, not allowed to abort the sweep:
the one broken check must not hide the state of the others.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from plugkit import Service

logger = logging.getLogger("pydsh.invariants")

#: A check: returns truthy when the invariant holds, or raises.
Predicate = Callable[[], Any]


class InvariantRegistry(Service):
    """Provides ``ctx.invariants``."""

    provide = "invariants"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._invariants: list[dict] = []

    def register(
        self, name: str, description: str, predicate: Predicate
    ) -> Callable[[], bool]:
        """Register a named check; returns a disposer.

        The description is not decoration — it is what an operator reads when
        the check fails, and it should say what *should* be true.
        """
        if not name:
            raise ValueError("an invariant needs a name")
        if not description:
            raise ValueError(
                f"invariant {name!r} needs a description saying what should be true; "
                "a failure with no description is a guarantee with no diagnosis"
            )
        entry = {"name": name, "description": description, "predicate": predicate}
        self._invariants.append(entry)

        def dispose() -> bool:
            for index, candidate in enumerate(self._invariants):
                if candidate is entry:
                    self._invariants.pop(index)
                    return True
            return False

        return dispose

    def names(self) -> list[str]:
        return [entry["name"] for entry in self._invariants]

    def check(self) -> dict:
        """Run every invariant. Reports which held and which did not."""
        passed: list[str] = []
        failed: list[dict] = []

        for entry in list(self._invariants):
            try:
                held = entry["predicate"]()
            except Exception as error:  # noqa: BLE001 - one broken check is not all of them
                failed.append({
                    "name": entry["name"],
                    "description": entry["description"],
                    "reason": f"the check raised {type(error).__name__}: {error}",
                })
                continue
            if held:
                passed.append(entry["name"])
            else:
                failed.append({
                    "name": entry["name"],
                    "description": entry["description"],
                    "reason": "the check returned false",
                })

        return {"passed": passed, "failed": failed, "ok": not failed}


__all__ = ["InvariantRegistry", "Predicate"]
