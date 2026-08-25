"""``/compact`` — the human entrance to the compaction seam.

The whole job is turning outcomes into sentences a person can act on. A refusal
carries a code precisely so this can route on it: matching on message text
would break the first time a message was reworded, and the message names
sequence numbers that mean nothing to the person who typed the command.
"""

from __future__ import annotations

from typing import Any

from plugkit import Service

from ..compaction import CompactionRefused
from ..operating.commands import CommandResult

USAGE = "Usage: /compact (no arguments)"

#: One sentence per refusal code. The conversation is untouched in every case,
#: and the attempt is in the log — both worth saying, because "nothing
#: happened" and "something half-happened" call for different next moves.
REFUSALS = {
    "empty": (
        "There is nothing to compact yet. The conversation is unchanged."
    ),
    "inverted": (
        "That range runs backwards, so nothing was compacted."
    ),
    "unbalanced": (
        "Compaction would have separated a tool call from its result. The "
        "conversation is unchanged; the attempt is recorded in the log."
    ),
    "changed": (
        "The history being compacted changed before it could be replaced. The "
        "conversation is unchanged; the attempt is recorded in the log."
    ),
    "commit": (
        "Compaction did not finish cleanly and some history may have changed. "
        "Check the session before retrying."
    ),
    "refused": (
        "Compaction was refused. The conversation is unchanged; the attempt is "
        "recorded in the log."
    ),
}

CANCELLED = "Compaction was cancelled."


class CompactCommand(Service):
    """Registers ``/compact``."""

    provide = "command_compact"
    inject = ["commands", "compaction"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        dispose = ctx.commands.register(
            "compact", "Summarise older history to make room", self._run
        )
        ctx.effect(lambda: dispose)

    async def _run(self, invocation: Any) -> CommandResult:
        if (invocation.raw_input or "").strip():
            return CommandResult.error(USAGE)
        if invocation.agent is None:
            return CommandResult.error("/compact needs a session to act on.")

        try:
            result = await self.ctx.compaction.compact_now(
                invocation.agent, invocation.signal, invocation.command_id
            )
        except CompactionRefused as refused:
            # Asked after the fact, not before: a compaction cancelled halfway
            # surfaces as whatever refusal the cancellation caused, and
            # reporting *that* would blame the mechanism for the person's own
            # interruption.
            if getattr(invocation.signal, "aborted", False):
                return CommandResult.error(CANCELLED)
            return CommandResult.error(
                REFUSALS.get(getattr(refused, "code", "refused"), REFUSALS["refused"])
            )

        if result is None:
            # An ordinary outcome, not a failure: a short conversation, or one
            # where every cut would split a tool pair.
            return CommandResult.success("There is no history to compact yet.")

        return CommandResult.success(
            f"Compacted {len(result.shadowed_seqs)} entries "
            f"(about {result.shadowed_tokens} tokens).",
            source_event_seq=result.summary_seq,
        )


__all__ = ["CompactCommand", "REFUSALS", "USAGE", "CANCELLED"]
