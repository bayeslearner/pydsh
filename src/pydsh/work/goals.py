"""``ctx.goals`` — intent that outlives a turn.

A conversation drifts. A goal is what was actually being pursued, recorded on
the session log so it survives a restart, a compaction, and the model losing
track of it.

There is no goal store. A goal is a fold over `goal/change` events, which is
what gets it durability, provenance and replay for free — and what makes
`arm`/`disarm` conspicuously different: **arming is never persisted.** "There
is a goal" and "this process is actively driving it" are different facts, and
writing the second to the log would mean a restart silently resumes pursuing
something nobody asked it to resume.
"""

from __future__ import annotations

import time
import uuid
import weakref
from typing import Any, Optional

from plugkit import Service

from ..dispatch import emit_contained
from ..session.projection import ProjectionDefinition
from .goal_fold import (
    GOAL_CHANGE_VERSION,
    GoalError,
    apply_goal_change,
    empty_goal_state,
    fold_goals,
)

#: The event a change lands as.
GOAL_CHANGE_EVENT = "goal/change"

#: Broadcast after a change commits.
GOAL_CHANGED = "goal/changed"

#: The key the goal projection owns.
GOAL_KEY = "goal"

#: How many turns one goal may drive before a human should look at it. A bound
#: on autonomy, not on the goal: an agent loop that has pursued one objective
#: for hundreds of rounds is usually stuck rather than thorough.
DEFAULT_MAX_GOAL_ROUNDS = 256


def new_goal_id() -> str:
    return uuid.uuid4().hex[:12]


def now_ms() -> int:
    return int(time.time() * 1000)


def create_change(text: str, at: Optional[int] = None) -> dict:
    """A change that starts a goal at revision 1."""
    stamp = now_ms() if at is None else at
    return {
        "version": GOAL_CHANGE_VERSION,
        "operation": "create",
        "goal": {
            "id": new_goal_id(),
            "revision": 1,
            "status": "active",
            "text": text,
            "created_at": stamp,
            "updated_at": stamp,
        },
    }


def next_change(current: dict, operation: str, text: Optional[str] = None) -> dict:
    """A change that follows ``current`` by exactly one revision.

    Built here rather than by a caller so the compare-and-set bookkeeping —
    which revision this follows, which fields carry forward — is in one place
    and cannot be got subtly wrong at each call site.
    """
    status = {
        "edit": current["status"],
        "pause": "paused",
        "resume": "active",
        "complete": "completed",
        "block": "blocked",
    }[operation]
    return {
        "version": GOAL_CHANGE_VERSION,
        "operation": operation,
        "goal": {
            **current,
            "revision": current["revision"] + 1,
            "status": status,
            "text": current["text"] if text is None else text,
            "updated_at": now_ms(),
        },
    }


def clear_change(current: dict) -> dict:
    """A tombstone retiring the current goal."""
    return {
        "version": GOAL_CHANGE_VERSION,
        "operation": "clear",
        "cleared": {"id": current["id"], "revision": current["revision"] + 1},
        "cleared_at": now_ms(),
    }


def _apply_projection(state: dict, event: Any) -> dict:
    if event.type != GOAL_CHANGE_EVENT:
        return state
    return apply_goal_change(state, event.data)


#: Registered when a projection registry is mounted, so a UI can watch a goal
#: without folding the log itself.
GOAL_PROJECTION = ProjectionDefinition(
    key=GOAL_KEY,
    init=empty_goal_state,
    apply=_apply_projection,
    view=lambda state: {
        "current": state["current"],
        "revision": state["current"]["revision"] if state["current"] else 0,
    },
)


class GoalService(Service):
    """Provides ``ctx.goals``."""

    provide = "goals"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        self.max_rounds = int(config.get("max_goal_rounds", DEFAULT_MAX_GOAL_ROUNDS))
        self._root = getattr(ctx, "root", ctx)
        # Process-local and never written: see the module docstring.
        self._armed: "weakref.WeakKeyDictionary[Any, dict]" = weakref.WeakKeyDictionary()

        projections = getattr(self._root, "session_projections", None)
        if projections is not None:
            release = projections.register(GOAL_PROJECTION)
            ctx.effect(lambda: release)

    # -- reading ----------------------------------------------------------- #
    def state(self, session: Any) -> dict:
        """The folded goal state for a session."""
        return fold_goals(
            [e.data for e in session.events if e.type == GOAL_CHANGE_EVENT]
        )

    def current(self, session: Any) -> Optional[dict]:
        """This session's goal, or ``None``."""
        return self.state(session)["current"]

    def history(self, session: Any) -> list:
        return self.state(session)["history"]

    # -- writing ----------------------------------------------------------- #
    def apply(self, session: Any, change: dict) -> dict:
        """Validate a change against the current fold and append it.

        Validated *before* appending, so a rejected change leaves no trace: the
        log is the source of truth and must not contain events that were never
        legal.
        """
        state = self.state(session)
        applied = apply_goal_change(state, change)  # raises GoalError
        session.append(GOAL_CHANGE_EVENT, change)
        emit_contained(self.ctx, GOAL_CHANGED, session, applied["current"])
        return applied["current"]

    def set(self, session: Any, text: str) -> dict:
        """Start a goal, or replace the text of the one already running."""
        current = self.current(session)
        if current is None or current["status"] in ("completed", "blocked"):
            return self.apply(session, create_change(text))
        return self.apply(session, next_change(current, "edit", text))

    def transition(self, session: Any, operation: str) -> dict:
        """Pause, resume, complete, or block the current goal."""
        current = self.current(session)
        if current is None:
            raise GoalError("there is no goal to change", "GOAL_NOT_FOUND")
        return self.apply(session, next_change(current, operation))

    def clear(self, session: Any) -> None:
        current = self.current(session)
        if current is None:
            raise GoalError("there is no goal to clear", "GOAL_NOT_FOUND")
        self.apply(session, clear_change(current))

    # -- arming (never persisted — I6) -------------------------------------- #
    def arm(self, session: Any) -> None:
        """Say this process should keep driving the session's goal.

        :raises GoalError: the round budget is spent. An agent that has pursued
            one objective for hundreds of turns is usually stuck rather than
            thorough, and continuing silently is how a loop becomes a bill.
        """
        entry = self._armed.setdefault(session, {"armed": False, "rounds": 0})
        if entry["rounds"] >= self.max_rounds:
            raise GoalError(
                f"this goal has driven {entry['rounds']} rounds, at the limit of "
                f"{self.max_rounds}; a human should look at it",
                "GOAL_ROUNDS_EXHAUSTED",
            )
        entry["armed"] = True
        entry["rounds"] += 1

    def disarm(self, session: Any) -> None:
        entry = self._armed.get(session)
        if entry is not None:
            entry["armed"] = False

    def is_armed(self, session: Any) -> bool:
        entry = self._armed.get(session)
        return bool(entry and entry["armed"])

    def rounds(self, session: Any) -> int:
        entry = self._armed.get(session)
        return entry["rounds"] if entry else 0


__all__ = [
    "GoalService",
    "GOAL_CHANGE_EVENT",
    "GOAL_CHANGED",
    "GOAL_KEY",
    "GOAL_PROJECTION",
    "DEFAULT_MAX_GOAL_ROUNDS",
    "create_change",
    "next_change",
    "clear_change",
    "new_goal_id",
]
