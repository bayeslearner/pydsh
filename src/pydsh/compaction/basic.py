"""The default compaction policy: summarise the oldest safe region.

Deliberately simple, because the interesting decision is not *which* region to
pick — it is that the region must be safe to cut at all. A smarter selector is
a different engine, which is what the interface is for.

The policy in three rules:

1. Never touch the most recent nodes. Summarising what the user just said would
   be absurd, and a keep-recent window is the cheapest way to guarantee it.
2. Within what remains, take the longest run whose edges are balanced cuts.
3. Ask the model for a summary, then commit it as one append that shadows the
   run.

A failure anywhere after step 3 begins is recorded in the log as a
``compaction/end`` carrying the error, because compaction usually runs
automatically and a capability that silently stops working is worse than one
that fails loudly.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from ..llm.chunks import ChunkType, GenerateOptions
from ..message import MessageSource, TextBlock, create_user_message, encode_payload
from ..session.pairing import balanced_after, balanced_before
from .engine import CompactionEngine, CompactionRefused, CompactionResult

logger = logging.getLogger("pydsh.compaction")

#: Surface tokens before an automatic compaction is considered worthwhile.
DEFAULT_THRESHOLD_TOKENS = 60_000

#: Surface nodes never compacted, however long the conversation gets.
DEFAULT_KEEP_RECENT_NODES = 8

#: Ceiling on the summary itself, so compaction cannot cost what it saves.
DEFAULT_SUMMARY_MAX_TOKENS = 1024

#: What the model is asked to do. A named constant rather than a literal: it is
#: the one piece of prompt text this service owns, and it is tunable (EP1).
SUMMARY_INSTRUCTION = (
    "Summarise the conversation so far. Preserve decisions made, facts "
    "established, file paths, identifiers, and anything the assistant "
    "committed to doing. Omit pleasantries and superseded detail. Write it as "
    "notes for whoever continues this conversation."
)

#: How the summary is introduced on the surface, so the model reads it as
#: context rather than as something the user just said.
CHECKPOINT_PREFIX = "[Earlier conversation, summarised]\n\n"


class BasicCompaction(CompactionEngine):
    """Summarise the oldest balanced region outside the keep-recent window."""

    inject = ["llm"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        self.threshold_tokens = int(
            config.get("threshold_tokens", DEFAULT_THRESHOLD_TOKENS)
        )
        self.keep_recent_nodes = int(
            config.get("keep_recent_nodes", DEFAULT_KEEP_RECENT_NODES)
        )
        self.summary_max_tokens = int(
            config.get("summary_max_tokens", DEFAULT_SUMMARY_MAX_TOKENS)
        )
        if self.keep_recent_nodes < 0:
            raise ValueError("keep_recent_nodes must not be negative")
        self._root = getattr(ctx, "root", ctx)

    # -- selection --------------------------------------------------------- #
    def _candidate_region(self, session: Any) -> Optional[tuple[int, int]]:
        """The longest balanced run outside the keep-recent window."""
        nodes = session.surface_nodes
        available = nodes[: max(0, len(nodes) - self.keep_recent_nodes)]
        if len(available) < 2:
            return None

        # Walk from the front: the oldest history is the least useful verbatim
        # and the most useful summarised.
        start = available[0]
        if not balanced_before(session, start):
            return None
        for end in reversed(available):
            if balanced_after(session, end):
                return (start, end)
        return None

    def _measure(self, session: Any, seqs: list[int]) -> int:
        """Token cost of some surface nodes, when a meter is mounted."""
        meter = getattr(self._root, "token_meter", None)
        if meter is None:
            return 0
        measured = meter.measure(session)
        by_seq = {node["seq"]: node["tokens"] for node in measured["nodes"]}
        return sum(by_seq.get(seq, 0) for seq in seqs)

    # -- the interface ----------------------------------------------------- #
    async def compact_if_needed(
        self, agent: Any, trigger: str, signal: Any = None
    ) -> Optional[CompactionResult]:
        meter = getattr(self._root, "token_meter", None)
        if meter is not None:
            total = meter.measure(agent.session)["total_tokens"]
            if total < self.threshold_tokens:
                return None
        return await self.compact_now(agent, signal)

    async def compact_now(
        self, agent: Any, signal: Any = None, source_command_id: Optional[str] = None
    ) -> Optional[CompactionResult]:
        region = self._candidate_region(agent.session)
        if region is None:
            # Ordinary: a short conversation, or one where every cut would
            # split a tool pair. Not a failure.
            return None
        return await self.compact_region(
            region[0], region[1], agent, signal, source_command_id
        )

    async def compact_region(
        self,
        start: int,
        end: int,
        agent: Any,
        signal: Any = None,
        source_command_id: Optional[str] = None,
    ) -> CompactionResult:
        session = agent.session
        nodes = session.surface_nodes
        shadowed = [seq for seq in nodes if start <= seq <= end]

        if not shadowed:
            raise CompactionRefused(
                f"no surface node lies in [{start}, {end}]; the surface holds {nodes}",
                "empty",
            )
        if start > end:
            raise CompactionRefused(f"region [{start}, {end}] is inverted", "inverted")
        if not balanced_before(session, shadowed[0]):
            raise CompactionRefused(
                f"the cut before surface node {shadowed[0]} would separate a tool "
                "call from its result",
                "unbalanced",
            )
        if not balanced_after(session, shadowed[-1]):
            raise CompactionRefused(
                f"the cut after surface node {shadowed[-1]} would separate a tool "
                "call from its result",
                "unbalanced",
            )

        compaction_id = uuid.uuid4().hex[:12]
        shadowed_tokens = self._measure(session, shadowed)

        start_event = session.append(
            "compaction/start",
            {
                "compaction_id": compaction_id,
                "region": {"start": shadowed[0], "end": shadowed[-1]},
                "source_command_id": source_command_id,
            },
        )

        try:
            summary = await self._summarise(agent, shadowed, signal)
        except Exception as error:  # noqa: BLE001 - recorded, then re-raised
            # Compaction usually runs on a schedule nobody is watching. A
            # failure that leaves no trace is a capability that silently stops
            # working, and the log is where an operator will look.
            session.append(
                "compaction/end",
                {"compaction_id": compaction_id, "error": f"{type(error).__name__}: {error}"},
            )
            raise

        summary_event = session.append(
            "compaction/summary",
            {
                "compaction_id": compaction_id,
                "summary": summary,
                "shadowed_range": {"start": shadowed[0], "end": shadowed[-1]},
                "shadowed_seqs": list(shadowed),
                "shadowed_tokens": shadowed_tokens,
                "provider": agent.options.provider,
                "model": agent.options.model,
            },
        )

        checkpoint = create_user_message(
            [TextBlock(CHECKPOINT_PREFIX + summary)],
            source=MessageSource("plugin", plugin="compaction", form="summary"),
        )
        checkpoint_event = session.append(
            "user/message",
            encode_payload(checkpoint),
            surface_op={"op": "replace", "start": shadowed[0], "end": shadowed[-1]},
            source_event_seqs=(start_event.seq, summary_event.seq, *shadowed),
        )
        session.append("compaction/end", {"compaction_id": compaction_id, "error": None})

        return CompactionResult(
            compaction_id=compaction_id,
            start_seq=start_event.seq,
            summary_seq=summary_event.seq,
            checkpoint_seq=checkpoint_event.seq,
            summary=summary,
            shadowed_seqs=list(shadowed),
            shadowed_tokens=shadowed_tokens,
            kept_tokens=self._measure(session, [checkpoint_event.seq]),
        )

    async def _summarise(self, agent: Any, shadowed: list[int], signal: Any) -> str:
        """Ask the model to summarise the messages about to be shadowed."""
        from ..message import decode_payload

        by_seq = {event.seq: event for event in agent.session.events}
        messages = []
        for seq in shadowed:
            event = by_seq[seq]
            message = agent.session.derive_event_message(event)
            if message is not None:
                messages.append(decode_payload(message))
        messages.append(
            create_user_message(
                [TextBlock(SUMMARY_INSTRUCTION)], source=MessageSource("plugin", plugin="compaction")
            )
        )

        options = GenerateOptions(
            provider=agent.options.provider,
            model=agent.options.model,
            messages=messages,
            max_tokens=self.summary_max_tokens,
            signal=signal,
            purpose="compaction",
        )
        parts: list[str] = []
        async for chunk in self.ctx.llm.stream(options):
            if chunk.type == ChunkType.TEXT_DELTA and chunk.text:
                parts.append(chunk.text)
        summary = "".join(parts).strip()
        if not summary:
            raise RuntimeError("compaction: the model returned an empty summary")
        return summary


__all__ = [
    "BasicCompaction",
    "DEFAULT_THRESHOLD_TOKENS",
    "DEFAULT_KEEP_RECENT_NODES",
    "DEFAULT_SUMMARY_MAX_TOKENS",
    "SUMMARY_INSTRUCTION",
    "CHECKPOINT_PREFIX",
]
