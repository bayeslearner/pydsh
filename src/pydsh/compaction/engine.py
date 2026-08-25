"""``ctx.compaction`` — replacing a stretch of history with a summary of it.

The interface, and the one rule every implementation must honour: **a region
may only be replaced if both its edges are balanced cuts**. Everything else —
when to compact, how much to keep, what a good summary looks like — is policy,
and policy is why this is an interface rather than a single class.

What a compaction actually does to the log is small. Three appends: a start
record, a summary record, and the checkpoint message that carries a surface
operation shadowing the region. The log grows; the surface shrinks; nothing is
deleted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from plugkit import Service


class CompactionRefused(RuntimeError):
    """A region cannot be compacted, and the surface is untouched."""


@dataclass(frozen=True)
class CompactionResult:
    """What one compaction did, for a caller that wants to report or audit it."""

    compaction_id: str
    start_seq: int
    summary_seq: int
    checkpoint_seq: int
    summary: str
    shadowed_seqs: list[int] = field(default_factory=list)
    shadowed_tokens: int = 0
    kept_tokens: int = 0

    @property
    def tokens_saved(self) -> int:
        """How much smaller the surface got. Negative if the summary was longer.

        Reported rather than enforced: a summary that costs more than what it
        replaced is unusual, not invalid, and refusing it would be a policy
        decision this layer does not own.
        """
        return self.shadowed_tokens - self.kept_tokens


class CompactionEngine(Service):
    """The seam. A consumer that wants another policy implements this."""

    provide = "compaction"

    async def compact_region(
        self, start: int, end: int, agent: Any, signal: Any = None
    ) -> CompactionResult:
        """Replace the surface nodes in ``[start, end]`` with one summary.

        :raises CompactionRefused: the region is empty, inverted, or its edges
            would cut a tool call away from its result.
        """
        raise NotImplementedError

    async def compact_now(
        self, agent: Any, signal: Any = None, source_command_id: Optional[str] = None
    ) -> Optional[CompactionResult]:
        """Compact whether or not the session is over its threshold.

        Returns ``None`` when no balanced region exists — which is an ordinary
        outcome, not a failure: a short conversation, or one where every cut
        would split a tool pair.
        """
        raise NotImplementedError

    async def compact_if_needed(
        self, agent: Any, trigger: str, signal: Any = None
    ) -> Optional[CompactionResult]:
        """Compact only if the session has grown past its threshold."""
        raise NotImplementedError


__all__ = ["CompactionEngine", "CompactionResult", "CompactionRefused"]
