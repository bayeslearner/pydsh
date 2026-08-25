"""Jobs and goals — work that outlives a step, intent that outlives a turn.

Two services with the same shape and nothing else in common: both are about
something persisting past the boundary a step or a turn would give it.

- ``ctx.jobs`` — background work, owned by the session that started it. The
  owner is a fence checked on every operation, and a job the caller does not
  own is reported as *absent* rather than forbidden.
- ``ctx.goals`` — a fold over ``goal/change`` events, with compare-and-set
  semantics so two writers cannot silently overwrite each other. Arming is
  process-local and never persisted.
"""

from .goal_fold import (
    FINISHED_STATUSES,
    GOAL_CHANGE_VERSION,
    GOAL_OPERATIONS,
    GOAL_STATUSES,
    GoalError,
    apply_goal_change,
    decode_goal_change,
    empty_goal_state,
    fold_goals,
    goal_change_ref,
)
from .goals import (
    DEFAULT_MAX_GOAL_ROUNDS,
    GOAL_CHANGE_EVENT,
    GOAL_CHANGED,
    GOAL_KEY,
    GOAL_PROJECTION,
    GoalService,
    clear_change,
    create_change,
    next_change,
)
from .jobs import (
    DEFAULT_MAX_BUFFER_BYTES,
    JOB_KINDS,
    JOB_STATUSES,
    TERMINAL_STATUSES,
    JobNotFound,
    JobRegistry,
    LocalJobs,
)
from .tools import GOAL_SCHEMA, JOB_START_SCHEMA, GoalTool, JobTools

__all__ = [
    # jobs
    "JobRegistry",
    "LocalJobs",
    "JobNotFound",
    "JobTools",
    "JOB_STATUSES",
    "TERMINAL_STATUSES",
    "JOB_KINDS",
    "DEFAULT_MAX_BUFFER_BYTES",
    "JOB_START_SCHEMA",
    # goals
    "GoalService",
    "GoalTool",
    "GoalError",
    "decode_goal_change",
    "goal_change_ref",
    "apply_goal_change",
    "fold_goals",
    "empty_goal_state",
    "create_change",
    "next_change",
    "clear_change",
    "GOAL_CHANGE_VERSION",
    "GOAL_STATUSES",
    "GOAL_OPERATIONS",
    "FINISHED_STATUSES",
    "GOAL_CHANGE_EVENT",
    "GOAL_CHANGED",
    "GOAL_KEY",
    "GOAL_PROJECTION",
    "GOAL_SCHEMA",
    "DEFAULT_MAX_GOAL_ROUNDS",
]
