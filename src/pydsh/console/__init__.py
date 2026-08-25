"""The console — the commands a person actually types.

The registry lives in :mod:`pydsh.operating.commands`; these are the default
commands registered on it. They sit in their own tier because they *drive* the
services below them: `/compact` reaches `ctx.compaction`, `/goal` reaches
`ctx.goals`, both of which are above the operating core. A command living
beside the registry it registers on would sit below the service it drives, and
that inversion only hurts later — when nothing in `operating` can move without
dragging compaction along with it.

Every one of them holds the same rule: a command never raises at the person who
typed it. Whatever goes wrong comes back as a `CommandResult` they can read.
"""

from .compact import CANCELLED, REFUSALS, CompactCommand
from .feedback import (
    DISCLOSURES,
    FEEDBACK_EVENT,
    UNCONFIGURED,
    FeedbackCommand,
    SessionFeedbackError,
    record_feedback,
    sharing_disclosure,
)
from .goal import CONTROLS, GoalCommand, parse_goal_command, render_goal

__all__ = [
    "CompactCommand",
    "REFUSALS",
    "CANCELLED",
    "GoalCommand",
    "parse_goal_command",
    "render_goal",
    "CONTROLS",
    "FeedbackCommand",
    "SessionFeedbackError",
    "record_feedback",
    "sharing_disclosure",
    "FEEDBACK_EVENT",
    "DISCLOSURES",
    "UNCONFIGURED",
]
