"""Schedules, hooks, and invariants — the harness reaching outside itself.

Three services that all involve something outside the loop having a say.

- ``ctx.schedules`` — durable reminders folded from the session log. The timer
  is a projection of that log and re-checks it on every wake-up, so a reminder
  never fires early. Delivery is session-local, and says so.
- ``ctx.hooks`` — a deployment's own commands at points in the loop. Merging
  several answers is conservative: any block wins, and nothing moves toward
  allow.
- ``ctx.invariants`` — named checks that make a violated assumption loud.
"""

from .hooks import (
    BLOCKING_EXIT_CODE,
    DECISIONS,
    DEFAULT_HOOK_TIMEOUT_MS,
    DEFAULT_STDERR_SUMMARY_MAX_CHARS,
    HookOutput,
    HooksProtocol,
    MergedOutcome,
    matches,
    merge_hook_outputs,
    parse_hook_output,
    summarize_stderr,
)
from .invariants import InvariantRegistry, Predicate
from .schedule import (
    MAX_TIMER_SECONDS,
    REMINDER_FORM,
    SCHEDULE_CHANGE_EVENT,
    SCHEDULE_SCHEMA,
    ScheduleRuntime,
    now_ms,
)
from .schedule_domain import (
    MIN_EVERY_INTERVAL_SECONDS,
    SCHEDULE_CHANGE_VERSION,
    SCHEDULE_KINDS,
    ScheduleError,
    create_after_record,
    create_at_record,
    create_change,
    create_every_record,
    decode_schedule_record,
    delete_change,
    fired_change,
    fold_schedules,
)

__all__ = [
    # schedules
    "ScheduleRuntime",
    "ScheduleError",
    "fold_schedules",
    "decode_schedule_record",
    "create_at_record",
    "create_after_record",
    "create_every_record",
    "create_change",
    "delete_change",
    "fired_change",
    "SCHEDULE_CHANGE_EVENT",
    "SCHEDULE_CHANGE_VERSION",
    "SCHEDULE_KINDS",
    "SCHEDULE_SCHEMA",
    "MIN_EVERY_INTERVAL_SECONDS",
    "MAX_TIMER_SECONDS",
    "REMINDER_FORM",
    "now_ms",
    # hooks
    "HooksProtocol",
    "HookOutput",
    "MergedOutcome",
    "matches",
    "parse_hook_output",
    "merge_hook_outputs",
    "summarize_stderr",
    "DEFAULT_HOOK_TIMEOUT_MS",
    "DEFAULT_STDERR_SUMMARY_MAX_CHARS",
    "BLOCKING_EXIT_CODE",
    "DECISIONS",
    # invariants
    "InvariantRegistry",
    "Predicate",
]
