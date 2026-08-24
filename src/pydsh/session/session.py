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
        self._events: list[SessionEvent] = list(seed_events)
        self._seq = max((e.seq for e in self._events), default=0)
        # Ordered seqs of surface events — the model-visible projection.
        self._surface_nodes: list[int] = [
            e.seq for e in self._events if e.type in SURFACE_EVENTS
        ]

    @property
    def seq(self) -> int:
        """The sequence number the next append will take (current tail)."""
        return self._seq

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
        if event_type in SURFACE_EVENTS:
            self._surface_nodes.append(event.seq)
        self.ctx.emit("session/event", self, event)
        return event

    def derive_event_message(self, event: SessionEvent) -> Any:
        """Project one surface event to its model-visible message, else None."""
        if event.type == "user/message":
            return event.data
        if event.type in ("assistant/message", "tool/result"):
            return event.data.get("message")
        return None

    def derive_messages(self) -> list[Any]:
        """The ordered model-visible messages, derived from surface events."""
        messages: list[Any] = []
        for event in self._events:
            if event.type in SURFACE_EVENTS:
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
