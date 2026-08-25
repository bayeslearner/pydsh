"""Making durability happen without anyone remembering to ask for it.

Sprint 01 made flushing explicit — ``sessions.flush(session)`` is the
checkpoint, and an acknowledged flush is on disk. What it did not do is call
it: the agent loop appends a whole conversation to memory and never flushes, so
until now a session reached disk only if the consumer remembered.

This policy watches turn boundaries and flushes every N of them. That is the
whole feature, and it is the difference between "durable" as a capability and
"durable" as something that happens.

Fail-soft on purpose: a flush that raises must not abort the turn that
triggered it. The events are still in memory, the next checkpoint retries, and
the failure is logged rather than swallowed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from plugkit import Service

logger = logging.getLogger("pydsh.checkpoint")

#: Turn boundaries between durable checkpoints. Low enough that a crash costs
#: at most a few turns; high enough that a long conversation is not rewriting
#: its tail constantly.
DEFAULT_EVERY_TURNS = 5

#: The event that marks a turn finished. Counting `turn/end` rather than
#: `assistant/message` means one checkpoint per turn however many steps it
#: took, and that the flush lands when the turn's last event is already in.
TURN_BOUNDARY = "turn/end"


class CheckpointPolicy(Service):
    """Provides ``ctx.checkpoint_policy`` — periodic durability, by turn count."""

    provide = "checkpoint_policy"
    inject = ["sessions"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        every_turns = config.get("every_turns", DEFAULT_EVERY_TURNS)
        if isinstance(every_turns, bool) or not isinstance(every_turns, int):
            raise TypeError(
                f"checkpoint policy every_turns must be an integer, got {every_turns!r}"
            )
        if every_turns < 1:
            raise ValueError(
                f"checkpoint policy every_turns must be positive, got {every_turns}"
            )
        self.every_turns = every_turns
        self._sessions = ctx.sessions
        self._turns: dict[str, int] = {}
        self._flushes: set[asyncio.Future] = set()
        ctx.on("session/event", self._on_event)

    def _on_event(self, session: Any, event: Any) -> None:
        if event.type != TURN_BOUNDARY:
            return
        session_id = session.id
        count = self._turns.get(session_id, 0) + 1
        self._turns[session_id] = count
        if count % self.every_turns:
            return
        if not self._sessions.has_persistence():
            # Nothing attached: not a failure, just a composition without
            # durability. Counting continues so attaching later works.
            return
        self._schedule(session)

    def _schedule(self, session: Any) -> None:
        """Start a flush without making the append that triggered it wait.

        ``session/event`` is a synchronous post-commit broadcast, so the flush
        cannot be awaited here. The task is tracked so it is not garbage
        collected mid-write and so a failure is reported rather than surfacing
        as asyncio's never-retrieved warning.
        """
        try:
            task = asyncio.ensure_future(self._sessions.flush(session))
        except RuntimeError:
            # No running loop — a synchronous caller appending outside async.
            # The events stay in memory and the next checkpoint retries.
            logger.debug("no running event loop; skipping checkpoint flush")
            return
        self._flushes.add(task)
        task.add_done_callback(self._finish)

    def _finish(self, task: asyncio.Future) -> None:
        self._flushes.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            # Fail-soft: the log is still in memory and the next checkpoint
            # will write it. Raising here would turn a durability hiccup into
            # a failed turn.
            logger.warning("checkpoint flush failed: %s", error, exc_info=error)

    async def drain(self) -> None:
        """Wait for any checkpoint in flight — for tests and for shutdown."""
        while self._flushes:
            await asyncio.gather(*list(self._flushes), return_exceptions=True)


__all__ = ["CheckpointPolicy", "DEFAULT_EVERY_TURNS", "TURN_BOUNDARY"]
