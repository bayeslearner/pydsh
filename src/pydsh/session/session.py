"""The append-only session log and its derived message list.

``Session`` is a plain class — it never imports the kernel. It owns the event
log for one conversation and the in-memory surface derived from it. ``append``
is the single writer; everything else reads.

Durability is explicit: ``append`` mutates memory (and emits ``session/event``);
a separate ``flush`` (on the store) writes the SQLite backend. The two are
deliberately not fused so a caller can choose its checkpoint boundary.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any

from ..dispatch import emit_contained
from .events import EVENT_DATA_FIELDS, SESSION_FORMAT_VERSION, SURFACE_EVENTS


class SessionError(RuntimeError):
    """Base error for the session module."""


class InvalidEventData(SessionError):
    """``append`` was given data that is not lossless-JSON."""


class UnknownEventType(SessionError):
    """``append`` was given an event type outside the vocabulary."""


@dataclass(frozen=True)
class SessionEvent:
    """One immutable entry in the session log.

    ``surface_op`` and ``source_event_seqs`` are present only on surface
    events (a ``surface_op`` of ``"append"``, or a replacement range once
    compaction exists). Log-only events never carry them.
    """

    type: str
    seq: int
    time: float
    data: Any
    surface_op: Any = "append"
    source_event_seqs: tuple[int, ...] = ()


@dataclass
class SessionHeader:
    """Storage metadata, kept out of the conversation events."""

    version: int = SESSION_FORMAT_VERSION
    id: str = ""
    created_at: float = 0.0
    cwd: str | None = None
    #: The call config the most recent step used, as a plain dict — the
    #: conversation's current route, not part of its content. Written by the
    #: agent loop that owns the epoch and read back on resume, so continuing a
    #: session does not silently change provider or model. ``None`` until a
    #: step has run.
    request: dict | None = None


def _validate_lossless_json(value: Any) -> None:
    """Reject data that ``json`` cannot round-trip byte-identically.

    ``json.dumps`` accepts ``float('nan')``/``inf`` and Python writes them as
    the null ``NaN/Infinity`` literals — which are not valid JSON and come back
    as ``nan``/``inf`` on parse. A cycle raises ``ValueError``. So: dump, parse,
    and compare; a mismatch means the value is not lossless-JSON. This is the
    trust boundary before any durable or derived state is touched.
    """
    if isinstance(value, bool) or value is None:
        return
    try:
        text = json.dumps(value, allow_nan=False)
        parsed = json.loads(text)
    except (TypeError, ValueError):
        raise InvalidEventData(
            "event data is not lossless-JSON "
            "(cycle, non-finite float, or unsupported scalar)"
        ) from None
    if parsed != value or _has_non_finite(value):
        raise InvalidEventData(
            "event data does not round-trip byte-identically (non-finite float)"
        )


def _has_non_finite(value: Any) -> bool:
    """True if any float anywhere in ``value`` is inf/NaN (would not round-trip)."""
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_has_non_finite(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_non_finite(v) for v in value)
    return False


class Session:
    """One conversation's append-only event log and its derived messages.

    A plain class; the ``SessionStore`` service holds live sessions and
    publishes their growth.
    """

    def __init__(
        self,
        ctx: Any,
        id: str,
        header: SessionHeader | None = None,
        seed_events: tuple[SessionEvent, ...] = (),
    ) -> None:
        self.ctx = ctx
        self.id = id
        self.header = header or SessionHeader(id=id)
        self._events: list[SessionEvent] = []
        self._seq = 0
        # Ordered seqs of surface events — the model-visible projection.
        self._surface_nodes: list[int] = []
        # Bumped on every replacement, so anything caching a view of the
        # surface has an exact, cheap staleness signal.
        self._replace_generation = 0
        # Provenance: which node replaced which. Kept for the life of the
        # session, so "what did this summary shadow" stays answerable.
        self._replacements: list[dict] = []
        for event in seed_events:
            self._seed(event)

    def _seed(self, event: SessionEvent) -> None:
        """Replay one stored event into the log and the surface.

        The surface is a *fold over the log's operations*, and it always was —
        filtering by event type happened to give the same answer while `append`
        was the only operation. Once a replacement exists, filtering resurrects
        exactly what compaction shadowed, and nothing reports it.
        """
        self._events.append(event)
        self._seq = max(self._seq, event.seq)
        operation = event.surface_op
        if isinstance(operation, dict) and operation.get("op") == "replace":
            self._apply_surface_replace(
                operation["start"], operation["end"], event.seq
            )
        elif event.type in SURFACE_EVENTS:
            self._surface_nodes.append(event.seq)

    @property
    def seq(self) -> int:
        """The sequence number of the last committed event."""
        return self._seq

    @property
    def replace_generation(self) -> int:
        """How many surface replacements have happened. A staleness signal."""
        return self._replace_generation

    @property
    def replacements(self) -> list[dict]:
        """Each replacement: the node that arrived and the nodes it shadowed."""
        return [dict(r) for r in self._replacements]

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        """Immutable view of the log, in append order."""
        return tuple(self._events)

    @property
    def surface_nodes(self) -> list[int]:
        """Seq numbers of surface events, in order — the derived projection."""
        return list(self._surface_nodes)

    def append(
        self,
        event_type: str,
        data: Any,
        *,
        surface_op: Any = "append",
        source_event_seqs: tuple[int, ...] = (),
    ) -> SessionEvent:
        """Append one event to the log.

        Validates the type is known and the data is lossless-JSON before any
        state changes, then records the event, adds surface events to the
        projection, and broadcasts ``session/event`` through the owning store.
        """
        replacing = isinstance(surface_op, dict) and surface_op.get("op") == "replace"
        if replacing and event_type not in SURFACE_EVENTS:
            raise SessionError(
                f"{event_type!r} cannot replace surface nodes: only surface events "
                f"({', '.join(SURFACE_EVENTS)}) have a place in the projection"
            )
        if event_type not in EVENT_DATA_FIELDS:
            raise UnknownEventType(
                f"unknown session event type {event_type!r}; "
                f"known: {sorted(EVENT_DATA_FIELDS)}"
            )
        _validate_lossless_json(data)

        self._seq += 1
        event = SessionEvent(
            type=event_type,
            seq=self._seq,
            time=time.time(),
            data=data,
            surface_op=surface_op if event_type in SURFACE_EVENTS else None,
            source_event_seqs=source_event_seqs,
        )
        self._events.append(event)
        if replacing:
            self._apply_surface_replace(
                surface_op["start"], surface_op["end"], event.seq
            )
        elif event_type in SURFACE_EVENTS:
            self._surface_nodes.append(event.seq)
        # Post-commit: the event is already in the log, so a throwing
        # observer must not turn a committed append into an exception.
        emit_contained(self.ctx, "session/event", self, event)
        return event

    def _apply_surface_replace(self, start: int, end: int, new_seq: int) -> None:
        """Swap the run of surface nodes from ``start`` to ``end`` for one node.

        ``start`` and ``end`` name the first and last *nodes* of a contiguous
        run, and the run is taken **positionally** — by where those nodes sit on
        the surface, not by comparing sequence numbers.

        That distinction only matters after the first replacement, which is
        exactly why it is easy to miss. A replacement puts a high sequence
        number where a low range used to be, so the surface stops being
        ordered by sequence: `[7, 4, 5, 6]` is a perfectly ordinary surface. A
        `start <= seq <= end` test then selects the wrong nodes or none at all,
        and compaction works precisely once per session.

        The swap is a single slice assignment, so the surface is never
        momentarily missing both the run and its replacement (I4).
        """
        nodes = self._surface_nodes
        try:
            first = nodes.index(start)
            last = nodes.index(end)
        except ValueError:
            raise SessionError(
                f"surface replace: node {start if start not in nodes else end} is "
                f"not on the surface; the surface holds {nodes}"
            ) from None
        if last < first:
            raise SessionError(
                f"surface replace: node {end} precedes {start} on the surface, so "
                f"[{start}, {end}] is not a run; the surface holds {nodes}"
            )
        shadowed = nodes[first : last + 1]
        self._surface_nodes[first : last + 1] = [new_seq]
        self._replace_generation += 1
        self._replacements.append(
            {"new_seq": new_seq, "shadowed_seqs": list(shadowed)}
        )

    def derive_event_message(self, event: SessionEvent) -> Any:
        """Project one surface event to its model-visible message, else None."""
        if event.type == "user/message":
            return event.data
        if event.type in ("assistant/message", "tool/result"):
            return event.data.get("message")
        return None

    def derive_messages(self) -> list[Any]:
        """The ordered model-visible messages, following the current surface.

        Driven by ``surface_nodes`` rather than by scanning for surface event
        types: after a compaction the two disagree, and the surface is the one
        that is right.
        """
        by_seq = {event.seq: event for event in self._events}
        messages: list[Any] = []
        for seq in self._surface_nodes:
            event = by_seq.get(seq)
            if event is None:
                raise SessionError(
                    f"surface node {seq} has no matching log event; the surface "
                    "is corrupt"
                )
            message = self.derive_event_message(event)
            if message is not None:
                messages.append(message)
        return messages

    # -- serialization used by the persistence backend --------------------

    def to_json(self) -> dict:
        """A JSON-safe snapshot: header + events, ready for the backend."""
        return {
            "header": {
                "version": self.header.version,
                "id": self.header.id,
                "created_at": self.header.created_at,
                "cwd": self.header.cwd,
                "request": self.header.request,
            },
            "events": [
                {
                    "type": e.type,
                    "seq": e.seq,
                    "time": e.time,
                    "data": e.data,
                    "surface_op": e.surface_op,
                    "source_event_seqs": list(e.source_event_seqs),
                }
                for e in self._events
            ],
        }

    @classmethod
    def from_json(cls, ctx: Any, payload: dict) -> "Session":
        """Rebuild a Session from a ``to_json`` snapshot, recomputing the surface."""
        header = SessionHeader(**payload["header"])
        events = tuple(
            SessionEvent(
                type=e["type"],
                seq=e["seq"],
                time=e["time"],
                data=e["data"],
                surface_op=e.get("surface_op"),
                source_event_seqs=tuple(e.get("source_event_seqs", ())),
            )
            for e in payload["events"]
        )
        return cls(ctx, header=header, id=header.id, seed_events=events)
