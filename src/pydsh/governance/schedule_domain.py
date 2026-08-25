"""Durable reminders: what one is, and what a log of changes folds to.

Pure functions. A schedule is a fold over `schedule/change` events, exactly
like a goal — durable, replayable, and needing no store of its own.

Two constraints are worth naming because they are refusals rather than
features. An interval below :data:`MIN_EVERY_INTERVAL_SECONDS` is rejected: a
one-second repeat is not a reminder, it is a busy loop with a prompt attached,
and each firing costs a model turn. And a one-shot in the past is rejected: a
reminder for a moment that has gone is a mistake, not an instruction.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

#: Bumped when the change shape changes.
SCHEDULE_CHANGE_VERSION = 1

#: The shortest repeat allowed. Five minutes: below that a "reminder" is a
#: loop, and the model pays for every firing.
MIN_EVERY_INTERVAL_SECONDS = 300

#: The instants a four-digit year can express, in milliseconds. Outside this a
#: timestamp is almost always a units mistake — seconds where milliseconds were
#: meant, or the other way round.
MIN_INSTANT_MS = int(datetime(1, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
MAX_INSTANT_MS = int(
    datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000
)

SCHEDULE_KINDS = ("at", "after", "every")

_RECORD_KEYS = frozenset({"id", "kind", "prompt", "scheduled_at", "every_seconds"})


class ScheduleError(ValueError):
    """A schedule that cannot be created or stored, with a code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def allocate_schedule_id() -> str:
    return uuid.uuid4().hex[:10]


def _prompt(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScheduleError("invalid_prompt", "a schedule needs a non-empty prompt")
    return value.strip()


def _instant(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScheduleError("invalid_rule", f"{field} must be an integer millisecond instant")
    if not (MIN_INSTANT_MS <= value <= MAX_INSTANT_MS):
        raise ScheduleError(
            "invalid_rule",
            f"{field} ({value}) is outside a four-digit year — check the units",
        )
    return value


def _future(target: int, now_ms: int, field: str) -> int:
    _instant(target, field)
    if target <= now_ms:
        # A reminder for a moment that has gone is a mistake, not an
        # instruction — and delivering it immediately would hide the mistake.
        raise ScheduleError("invalid_rule", f"{field} is in the past")
    return target


def create_at_record(prompt: str, at_ms: int, now_ms: int) -> dict:
    """A one-shot reminder at an instant."""
    return {
        "id": allocate_schedule_id(),
        "kind": "at",
        "prompt": _prompt(prompt),
        "scheduled_at": _future(at_ms, now_ms, "at_ms"),
        "every_seconds": None,
    }


def create_after_record(prompt: str, after_seconds: int, now_ms: int) -> dict:
    """A one-shot reminder after a delay."""
    if isinstance(after_seconds, bool) or not isinstance(after_seconds, int) or after_seconds <= 0:
        raise ScheduleError("invalid_rule", "after_seconds must be a positive integer")
    return {
        "id": allocate_schedule_id(),
        "kind": "after",
        "prompt": _prompt(prompt),
        "scheduled_at": _future(now_ms + after_seconds * 1000, now_ms, "after_seconds"),
        "every_seconds": None,
    }


def create_every_record(prompt: str, every_seconds: int, now_ms: int) -> dict:
    """A repeating reminder, floored at the minimum interval."""
    if isinstance(every_seconds, bool) or not isinstance(every_seconds, int):
        raise ScheduleError("invalid_rule", "every_seconds must be an integer")
    if every_seconds < MIN_EVERY_INTERVAL_SECONDS:
        raise ScheduleError(
            "frequency_too_high",
            f"every_seconds must be at least {MIN_EVERY_INTERVAL_SECONDS} "
            f"({every_seconds} would cost a model turn every {every_seconds}s)",
        )
    return {
        "id": allocate_schedule_id(),
        "kind": "every",
        "prompt": _prompt(prompt),
        "scheduled_at": _future(now_ms + every_seconds * 1000, now_ms, "every_seconds"),
        "every_seconds": every_seconds,
    }


def decode_schedule_record(value: Any) -> dict:
    """Validate a stored record, or say what is wrong with it."""
    if not isinstance(value, dict):
        raise ScheduleError("invalid_rule", "a schedule record must be an object")
    unknown = set(value) - _RECORD_KEYS
    if unknown:
        raise ScheduleError(
            "invalid_rule", f"schedule record has unexpected field(s) {', '.join(sorted(unknown))}"
        )
    kind = value.get("kind")
    if kind not in SCHEDULE_KINDS:
        raise ScheduleError(
            "invalid_rule",
            f"schedule kind {kind!r} is unknown; expected one of {', '.join(SCHEDULE_KINDS)}",
        )
    identifier = value.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise ScheduleError("invalid_rule", "schedule id must be a non-empty string")

    record = {
        "id": identifier,
        "kind": kind,
        "prompt": _prompt(value.get("prompt")),
        "scheduled_at": _instant(value.get("scheduled_at"), "scheduled_at"),
        "every_seconds": value.get("every_seconds"),
    }
    if kind == "every":
        every = record["every_seconds"]
        if isinstance(every, bool) or not isinstance(every, int) or every < MIN_EVERY_INTERVAL_SECONDS:
            raise ScheduleError(
                "frequency_too_high",
                f"a repeating schedule needs every_seconds >= {MIN_EVERY_INTERVAL_SECONDS}",
            )
    elif record["every_seconds"] is not None:
        raise ScheduleError("invalid_rule", f"a {kind!r} schedule must not carry every_seconds")
    return record


def create_change(record: dict) -> dict:
    return {"version": SCHEDULE_CHANGE_VERSION, "operation": "create", "record": record}


def delete_change(schedule_id: str) -> dict:
    return {"version": SCHEDULE_CHANGE_VERSION, "operation": "delete", "id": schedule_id}


def fired_change(schedule_id: str, fired_at: int, next_at: Optional[int] = None) -> dict:
    """A firing. ``next_at`` advances a repeating schedule; absent completes it."""
    return {
        "version": SCHEDULE_CHANGE_VERSION,
        "operation": "fired",
        "id": schedule_id,
        "fired_at": fired_at,
        "next_at": next_at,
    }


def fold_schedules(changes: list, now_ms: int) -> dict:
    """Replay a log of changes into the active schedules.

    Anything whose target has passed is reported as **overdue** rather than
    dropped: the session was not running when it came due, and the caller needs
    to know that when it delivers late.
    """
    active: dict[str, dict] = {}
    for change in changes:
        if not isinstance(change, dict):
            continue
        if change.get("version") != SCHEDULE_CHANGE_VERSION:
            raise ScheduleError(
                "invalid_rule",
                f"schedule change version {change.get('version')!r} is not "
                f"{SCHEDULE_CHANGE_VERSION}",
            )
        operation = change.get("operation")
        if operation == "create":
            record = decode_schedule_record(change["record"])
            active[record["id"]] = record
        elif operation == "delete":
            active.pop(change.get("id"), None)
        elif operation == "fired":
            existing = active.get(change.get("id"))
            if existing is None:
                continue
            next_at = change.get("next_at")
            if next_at is None:
                active.pop(existing["id"], None)  # a one-shot is done
            else:
                active[existing["id"]] = {**existing, "scheduled_at": next_at}
        else:
            raise ScheduleError(
                "invalid_rule", f"schedule change operation {operation!r} is unknown"
            )

    ordered = sorted(active.values(), key=lambda r: (r["scheduled_at"], r["id"]))
    return {
        "active": [r for r in ordered if r["scheduled_at"] > now_ms],
        "overdue": [r for r in ordered if r["scheduled_at"] <= now_ms],
    }


__all__ = [
    "SCHEDULE_CHANGE_VERSION",
    "MIN_EVERY_INTERVAL_SECONDS",
    "MIN_INSTANT_MS",
    "MAX_INSTANT_MS",
    "SCHEDULE_KINDS",
    "ScheduleError",
    "allocate_schedule_id",
    "create_at_record",
    "create_after_record",
    "create_every_record",
    "decode_schedule_record",
    "create_change",
    "delete_change",
    "fired_change",
    "fold_schedules",
]
