"""Plan mode — a recorded collaboration state the model works under.

While it is on, the deployment's guidance joins the system prompt and
`exit_plan_mode` puts the finished plan to the user. The state is one thing:
the last `plan/mode` event on the session log, so a resumed session has it
without an in-memory mirror to rebuild or keep in sync.
"""

from .mode import (
    EXIT_DESCRIPTION,
    EXIT_PLAN_MODE,
    EXIT_SCHEMA,
    PLAN_EVENT,
    PLAN_KEY,
    PLAN_PROJECTION,
    POLICY_ORDER,
    POLICY_SECTION,
    USAGE,
    PlanMode,
    PlanModeError,
    last_turn_start_seq,
    fold_plan_mode,
    has_open_turn,
)

__all__ = [
    "PlanMode",
    "PlanModeError",
    "fold_plan_mode",
    "has_open_turn",
    "last_turn_start_seq",
    "PLAN_PROJECTION",
    "PLAN_EVENT",
    "PLAN_KEY",
    "EXIT_PLAN_MODE",
    "EXIT_SCHEMA",
    "EXIT_DESCRIPTION",
    "POLICY_SECTION",
    "POLICY_ORDER",
    "USAGE",
]
