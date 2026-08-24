"""The agent's pending-input queues, projected from the session log.

Delivering a message to an agent and *processing* it are separate moments. The
inbox is what sits between them: two ordered queues holding input that has
arrived but has not reached a turn boundary yet.

``next-turn`` holds prompts waiting to open a turn of their own; ``next-step``
holds input to fold into the next step boundary of a turn already running.

The queues are a **projection**, not a store. Every change appends an
``agent/inbox/spliced`` event *before* memory is touched, so the log alone can
rebuild them — which is what makes "the user typed it, then the process died"
recoverable rather than silently lost. :meth:`Inbox.replay` is that rebuild,
and the test that it equals the live queues is the proof the projection is
faithful.

The splice shape mirrors the reference (and `Array.prototype.splice` before
it): ``target``, ``start``, ``inserted``, plus ``removedCount`` when entries
were taken out and ``outcome: "canceled"`` when they were taken out without
replacement — the difference between claiming input and cancelling it.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from ..message import decode_payload, encode_payload

#: The two queues. ``next-turn`` opens a turn; ``next-step`` joins one.
NEXT_TURN = "next-turn"
NEXT_STEP = "next-step"
TARGETS = (NEXT_TURN, NEXT_STEP)

#: The session event every change is recorded as.
SPLICE_EVENT = "agent/inbox/spliced"


class Inbox:
    """Input that has been delivered to an agent but not yet processed."""

    def __init__(
        self,
        session: Any,
        notifications: Optional[dict[str, Callable[..., None]]] = None,
    ) -> None:
        self.session = session
        self._queues: dict[str, list[Any]] = {NEXT_TURN: [], NEXT_STEP: []}
        # inserted / discarded / claimed — the agent turns these into events on
        # the context. Optional so an Inbox is usable without an agent.
        self.notifications = notifications or {}

    # -- read-only views --------------------------------------------------- #
    @property
    def next_turn(self) -> list[Any]:
        """Prompts waiting to open a turn, in order."""
        return list(self._queues[NEXT_TURN])

    @property
    def next_step(self) -> list[Any]:
        """Input waiting for the next step boundary, in order."""
        return list(self._queues[NEXT_STEP])

    @property
    def has_pending(self) -> bool:
        """Whether either queue holds anything."""
        return bool(self._queues[NEXT_TURN] or self._queues[NEXT_STEP])

    # -- changes ----------------------------------------------------------- #
    def _queue(self, target: str) -> list[Any]:
        """The named queue, or a clear error naming the two that exist."""
        queue = self._queues.get(target)
        if queue is None:
            raise ValueError(f"unknown inbox target {target!r}; known: {TARGETS}")
        return queue

    def append(self, target: str, message: Any) -> None:
        """Add a message to the end of a queue."""
        self._splice(target, len(self._queue(target)), 0, [message])

    def prepend(self, target: str, message: Any) -> None:
        """Add a message to the front of a queue."""
        self._splice(target, 0, 0, [message])

    def claim(self, target: str, turn: int) -> list[Any]:
        """Take the batch to process next.

        Always drains ``next-step`` in full — that input was delivered for
        *this* turn's next boundary. When ``target`` is ``next-turn`` it also
        takes the single head of ``next-turn``, because each queued prompt
        opens its own turn rather than being merged with its neighbours.
        """
        claimed = self._splice(NEXT_STEP, 0, len(self._queues[NEXT_STEP]), [])
        if target == NEXT_TURN:
            claimed.extend(self._splice(NEXT_TURN, 0, 1, []))
        notify = self.notifications.get("claimed")
        if notify:
            for message in claimed:
                notify(message, turn)
        return claimed

    def remove(self, message_id: str) -> bool:
        """Cancel one waiting message by id; True when it was found."""
        for target in (NEXT_STEP, NEXT_TURN):
            queue = self._queues[target]
            index = next(
                (i for i, m in enumerate(queue) if getattr(m, "id", None) == message_id),
                -1,
            )
            if index < 0:
                continue
            removed = self._splice(target, index, 1, [])
            notify = self.notifications.get("discarded")
            if notify and removed:
                notify(removed[0])
            return True
        return False

    def clear(self) -> None:
        """Drop everything waiting, without claiming it."""
        self._splice(NEXT_STEP, 0, len(self._queues[NEXT_STEP]), [])
        self._splice(NEXT_TURN, 0, len(self._queues[NEXT_TURN]), [])

    # -- the one mutation path --------------------------------------------- #
    def _splice(
        self,
        target: str,
        start: int,
        removed_count: int,
        inserted: list[Any],
    ) -> list[Any]:
        """Record a change, then apply it. Returns what was removed.

        A no-op splice (nothing in, nothing out) writes no event: clearing an
        already-empty queue is not something that happened, and logging it
        would put noise in a log whose whole value is that it is the story.
        """
        queue = self._queue(target)
        if not inserted and not removed_count:
            return []

        splice: dict[str, Any] = {
            "target": target,
            "start": start,
            "inserted": [encode_payload(m) for m in inserted],
        }
        if removed_count:
            splice["removedCount"] = removed_count
            if not inserted:
                # Removed with nothing put back: the entries were taken out of
                # play. A claim is the same shape — the consumer distinguishes
                # them by what happens next in the log, as the reference does.
                splice["outcome"] = "canceled"
        # The event is the commit: it lands before memory changes, so a
        # failure to record cannot leave the queues ahead of the log.
        self.session.append(SPLICE_EVENT, splice)

        removed = queue[start : start + removed_count]
        queue[start : start + removed_count] = list(inserted)
        notify = self.notifications.get("inserted")
        if notify:
            for message in inserted:
                notify(message)
        return removed

    # -- the projection ---------------------------------------------------- #
    @classmethod
    def replay(cls, session: Any) -> "Inbox":
        """Rebuild the queues from a session's splice events.

        Applied without notifications and without re-appending: this is a read
        of history, not a repeat of it.
        """
        inbox = cls(session)
        for event in session.events:
            if event.type != SPLICE_EVENT:
                continue
            data = event.data
            queue = inbox._queues[data["target"]]
            start = data["start"]
            removed_count = data.get("removedCount", 0)
            inserted = [decode_payload(m) for m in data["inserted"]]
            queue[start : start + removed_count] = inserted
        return inbox


__all__ = ["Inbox", "NEXT_TURN", "NEXT_STEP", "TARGETS", "SPLICE_EVENT"]
