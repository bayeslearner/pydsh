"""Compaction — replacing a stretch of history with a summary of it.

The log is append-only and immutable; the **surface** is the projection of it
the model sees. Compaction appends one event that declares it shadows a range
of surface nodes. Nothing is deleted, and a reader who wants the original still
has it at its original sequence.

The constraint that shapes it: a region may only be replaced if both its edges
are **balanced cuts** — points where no tool call is still awaiting its result.
Cut elsewhere and the model is shown a request that was never answered.
"""

from .basic import (
    CHECKPOINT_PREFIX,
    DEFAULT_KEEP_RECENT_NODES,
    DEFAULT_SUMMARY_MAX_TOKENS,
    DEFAULT_THRESHOLD_TOKENS,
    SUMMARY_INSTRUCTION,
    BasicCompaction,
)
from .engine import (
    REFUSAL_CODES,
    CompactionEngine,
    CompactionRefused,
    CompactionResult,
)

__all__ = [
    "CompactionEngine",
    "CompactionResult",
    "CompactionRefused",
    "REFUSAL_CODES",
    "BasicCompaction",
    "DEFAULT_THRESHOLD_TOKENS",
    "DEFAULT_KEEP_RECENT_NODES",
    "DEFAULT_SUMMARY_MAX_TOKENS",
    "SUMMARY_INSTRUCTION",
    "CHECKPOINT_PREFIX",
]
