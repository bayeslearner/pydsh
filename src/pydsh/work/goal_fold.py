"""Goal changes: what one looks like, and what a log of them folds to.

Pure functions, no service, no I/O. A goal is not a store — it is a fold over
`goal/change` events on the session log, which is why it survives a restart, a
compaction, and the model forgetting, without anything else being persisted.

Two rules do the work:

**Every change carries the whole goal.** Never a delta. A full-value event is
self-describing: a reader can interpret one without replaying what came before,
and a transition is validated by comparing two complete states rather than by
reconstructing one.

**Every change names the revision it follows.** Compare-and-set, without a
lock. Two writers racing cannot silently overwrite each other — the second
names a revision that is no longer current and is told so, which is a bug
report rather than lost intent.
"""

from __future__ import annotations

from typing import Any, Optional

#: Bumped when the change shape changes, so a stored change from another
#: version is refused rather than misread.
GOAL_CHANGE_VERSION = 1

#: Where a goal can be.
GOAL_STATUSES = ("active", "paused", "completed", "blocked")

#: Statuses from which a goal no longer drives anything, so a new one may start.
FINISHED_STATUSES = frozenset({"completed", "blocked"})

#: Operations carrying a full snapshot, and the tombstone that carries none.
SNAPSHOT_OPERATIONS = frozenset(
    {"create", "edit", "pause", "resume", "complete", "block"}
)
GOAL_OPERATIONS = SNAPSHOT_OPERATIONS | {"clear"}

_SNAPSHOT_KEYS = frozenset({"version", "operation", "goal"})
_CLEAR_KEYS = frozenset({"version", "operation", "cleared", "cleared_at"})
_GOAL_KEYS = frozenset(
    {"id", "revision", "status", "text", "created_at", "updated_at"}
)
_REF_KEYS = frozenset({"id", "revision"})


class GoalError(ValueError):
    """A change that cannot be applied, with a code a caller can match on."""

    def __init__(self, message: str, code: str = "GOAL_INVALID_CHANGE") -> None:
        super().__init__(message)
        self.code = code


def _exact_keys(value: dict, expected: frozenset, what: str) -> None:
    """Reject a missing *or* an extra key.

    Extras matter as much as omissions: a change carrying a field this version
    does not know is a change written against a different contract, and
    silently ignoring it would apply half of what the writer intended.
    """
    actual = set(value)
    if actual != set(expected):
        missing = sorted(set(expected) - actual)
        extra = sorted(actual - set(expected))
        parts = []
        if missing:
            parts.append(f"missing {', '.join(missing)}")
        if extra:
            parts.append(f"unexpected {', '.join(extra)}")
        raise GoalError(f"goal {what}: {'; '.join(parts)}")


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GoalError(f"goal change {field} must be a positive integer, got {value!r}")
    return value


def _non_negative(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise GoalError(f"goal change {field} must be non-negative, got {value!r}")
    return value


def _decode_goal(value: Any) -> dict:
    """One complete goal snapshot."""
    if not isinstance(value, dict):
        raise GoalError("goal change goal must be an object")
    _exact_keys(value, _GOAL_KEYS, "snapshot")

    identifier = value["id"]
    if not isinstance(identifier, str) or not identifier:
        raise GoalError("goal id must be a non-empty string")
    status = value["status"]
    if status not in GOAL_STATUSES:
        raise GoalError(
            f"goal status {status!r} is unknown; expected one of {', '.join(GOAL_STATUSES)}"
        )
    text = value["text"]
    if not isinstance(text, str) or not text.strip():
        raise GoalError("goal text must be a non-empty string")

    created_at = _non_negative(value["created_at"], "created_at")
    updated_at = _non_negative(value["updated_at"], "updated_at")
    if updated_at < created_at:
        # Not pedantry: a goal updated before it existed means the clock or the
        # writer is wrong, and every later "which is newer" comparison inherits
        # the mistake.
        raise GoalError("goal updated_at is before created_at")

    return {
        "id": identifier,
        "revision": _positive_int(value["revision"], "revision"),
        "status": status,
        "text": text,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _decode_ref(value: Any, what: str) -> dict:
    if not isinstance(value, dict):
        raise GoalError(f"goal change {what} must be an object")
    _exact_keys(value, _REF_KEYS, what)
    identifier = value["id"]
    if not isinstance(identifier, str) or not identifier:
        raise GoalError(f"goal change {what} id must be a non-empty string")
    return {"id": identifier, "revision": _positive_int(value["revision"], f"{what}.revision")}


def decode_goal_change(value: Any) -> dict:
    """Validate one change, or say exactly what is wrong with it."""
    if not isinstance(value, dict):
        raise GoalError("a goal change must be an object")
    if value.get("version") != GOAL_CHANGE_VERSION:
        raise GoalError(
            f"goal change version {value.get('version')!r} is not "
            f"{GOAL_CHANGE_VERSION}; refusing to guess its shape"
        )

    operation = value.get("operation")
    if operation not in GOAL_OPERATIONS:
        raise GoalError(
            f"goal change operation {operation!r} is unknown; expected one of "
            f"{', '.join(sorted(GOAL_OPERATIONS))}"
        )

    if operation == "clear":
        _exact_keys(value, _CLEAR_KEYS, "clear change")
        return {
            "version": GOAL_CHANGE_VERSION,
            "operation": "clear",
            "cleared": _decode_ref(value["cleared"], "cleared"),
            "cleared_at": _non_negative(value["cleared_at"], "cleared_at"),
        }

    _exact_keys(value, _SNAPSHOT_KEYS, "snapshot change")
    return {
        "version": GOAL_CHANGE_VERSION,
        "operation": operation,
        "goal": _decode_goal(value["goal"]),
    }


def goal_change_ref(change: dict) -> dict:
    """The revision identity a change carries, snapshot or tombstone."""
    if change["operation"] == "clear":
        return dict(change["cleared"])
    return {"id": change["goal"]["id"], "revision": change["goal"]["revision"]}


def empty_goal_state() -> dict:
    """The fold's accumulator for a log with no goal changes."""
    return {"current": None, "history": []}


def apply_goal_change(state: dict, raw: Any) -> dict:
    """Fold one change onto the state, validating the transition.

    :raises GoalError: the change is malformed, or does not follow the current
        goal by exactly one revision.
    """
    change = decode_goal_change(raw)
    current = state["current"]
    ref = goal_change_ref(change)

    if change["operation"] == "create":
        if current is not None and current["status"] not in FINISHED_STATUSES:
            # Two live goals means "what are we doing" has two answers, and
            # nothing downstream can pick between them.
            raise GoalError(
                f"a goal is already active ({current['id']}, revision "
                f"{current['revision']}); complete or block it before creating another",
                "GOAL_ALREADY_ACTIVE",
            )
        if change["goal"]["revision"] != 1:
            raise GoalError("a created goal must start at revision 1")
        history = [*state["history"], change]
        return {"current": change["goal"], "history": history}

    if current is None:
        raise GoalError("there is no goal to change", "GOAL_NOT_FOUND")
    if ref["id"] != current["id"]:
        raise GoalError(
            f"this change names goal {ref['id']}, but the current goal is {current['id']}",
            "GOAL_NOT_FOUND",
        )
    if ref["revision"] != current["revision"] + 1:
        # Compare-and-set: the loser of a race is told, rather than silently
        # overwriting the winner.
        raise GoalError(
            f"this change follows revision {ref['revision'] - 1}, but the goal is "
            f"at revision {current['revision']} — re-read and try again",
            "GOAL_STALE_REVISION",
        )

    history = [*state["history"], change]
    if change["operation"] == "clear":
        return {"current": None, "history": history}
    return {"current": change["goal"], "history": history}


def fold_goals(changes: list) -> dict:
    """Replay a log of changes into the current goal and its history."""
    state = empty_goal_state()
    for change in changes:
        state = apply_goal_change(state, change)
    return state


__all__ = [
    "GOAL_CHANGE_VERSION",
    "GOAL_STATUSES",
    "GOAL_OPERATIONS",
    "SNAPSHOT_OPERATIONS",
    "FINISHED_STATUSES",
    "GoalError",
    "decode_goal_change",
    "goal_change_ref",
    "apply_goal_change",
    "fold_goals",
    "empty_goal_state",
]
