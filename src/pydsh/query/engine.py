"""``ctx.session_query`` — the corpus, searchable.

Reads *storage*, not live sessions. The corpus is everything that ever ran and
most of it is not live; instantiating three hundred sessions to count them
costs about what replaying the month costs, and a search has to be cheaper than
the thing it searches.

Read-only throughout. Nothing here writes, so nothing here can corrupt a log.
"""

from __future__ import annotations

from typing import Any, Optional

from plugkit import Service

from ..message.blocks import Message, TextBlock
from ..message.payload import decode_payload
from .filters import (
    QueryError,
    apply_event_filters,
    apply_session_filters,
    materialise_event_filters,
    materialise_session_filters,
)


def message_text(value: Any) -> str:
    """The readable text of a message, however it was stored."""
    decoded = decode_payload(value)
    if isinstance(decoded, Message):
        return " ".join(
            block.text for block in decoded.content if isinstance(block, TextBlock)
        )
    if isinstance(decoded, dict):
        content = decoded.get("content")
        if isinstance(content, str):
            return content
    return ""


def event_text(event: Any) -> str:
    """What a search should look inside for this event.

    Messages, not payloads: a user searching for a phrase wants what was
    *said*, not the JSON it was stored in — and searching the raw payload would
    match on field names and encoding tags.
    """
    data = event.data
    if not isinstance(data, dict):
        return str(data) if isinstance(data, str) else ""
    if event.type == "user/message":
        return message_text(data)
    if event.type in ("assistant/message", "tool/result"):
        return message_text(data.get("message"))
    if event.type == "tool/call":
        return f"{data.get('name', '')} {data.get('arguments', '')}"
    return ""


def classify_surface(events: list, surface_nodes: list[int], surface_types: tuple) -> dict:
    """Where each event sits relative to what the model can currently see.

    Three classes, and the middle one only exists because of compaction: an
    event that *would* be on the surface, replaced by a summary that shadows
    it. Before sprint 10, "what can the model see" and "what ever happened"
    were the same question.
    """
    current = set(surface_nodes)
    classes: dict[int, str] = {}
    for event in events:
        if event.seq in current:
            classes[event.seq] = "current"
        elif event.type in surface_types:
            classes[event.seq] = "shadowed"
        else:
            classes[event.seq] = "log-only"
    return classes


class SessionCorpus:
    """Every session there is, read through storage rather than instantiated."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def _persistence(self) -> Any:
        backend = self.ctx.sessions.persistence
        if backend is None:
            raise QueryError(
                "no persistence backend is attached, so there is no corpus to search",
                "SESSION_QUERY_NO_CORPUS",
            )
        return backend

    async def list_sessions(self) -> list[dict]:
        """Every session's summary, newest first. No event bodies are read."""
        backend = self._persistence()
        live = {s.id: s for s in self.ctx.sessions.list()}
        records: dict[str, dict] = {}

        for session_id in await backend.list():
            tail = await backend.read_from(session_id, 10**18)  # header only
            header = tail["meta"] if tail else None
            records[session_id] = {
                "id": session_id,
                "created_at": getattr(header, "created_at", 0.0),
                "cwd": getattr(header, "cwd", None),
                "availability": ["persisted"],
            }

        for session_id, session in live.items():
            record = records.get(session_id)
            if record is None:
                records[session_id] = {
                    "id": session_id,
                    "created_at": session.header.created_at,
                    "cwd": session.header.cwd,
                    "availability": ["live"],
                }
            else:
                # Both: a client choosing where to read from needs to know.
                record["availability"] = ["live", "persisted"]

        return sorted(
            records.values(),
            key=lambda r: (-(r["created_at"] or 0), r["id"]),
        )

    async def load(self, session_id: str) -> dict:
        """One session's header and events."""
        live = self.ctx.sessions.get(session_id)
        if live is not None:
            return {"header": live.header, "events": list(live.events),
                    "surface_nodes": live.surface_nodes}

        backend = self._persistence()
        tail = await backend.read_from(session_id, 1)
        if tail is None:
            raise QueryError(f"no session {session_id!r}", "SESSION_QUERY_NOT_FOUND")

        # Rebuild the surface by replaying operations — the same rule spec 10
        # established, and for the same reason: filtering by event type would
        # report a compacted session as though it had never been compacted.
        from ..session.session import Session

        rebuilt = Session(None, id=session_id, header=tail["meta"],
                          seed_events=tuple(tail["events"]))
        return {"header": tail["meta"], "events": list(tail["events"]),
                "surface_nodes": rebuilt.surface_nodes}


class SessionQueryEngine(Service):
    """Provides ``ctx.session_query``."""

    provide = "session_query"
    inject = ["sessions"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self.corpus = SessionCorpus(ctx)

    async def list_sessions(self) -> list[dict]:
        return await self.corpus.list_sessions()

    async def read_session(self, session_id: str) -> dict:
        loaded = await self.corpus.load(session_id)
        return {
            "id": session_id,
            "header": loaded["header"],
            "events": loaded["events"],
        }

    async def read_surface(self, session_id: str) -> list[dict]:
        """Only what the model can currently see."""
        return [d for d in await self.list_events(session_id) if d["surface"] == "current"]

    async def list_events(self, session_id: str) -> list[dict]:
        """Every event as a filterable document."""
        from ..session.events import SURFACE_EVENTS

        loaded = await self.corpus.load(session_id)
        classes = classify_surface(
            loaded["events"], loaded["surface_nodes"], SURFACE_EVENTS
        )
        return [
            {
                "session_id": session_id,
                "seq": event.seq,
                "time": event.time,
                "type": event.type,
                "surface": classes[event.seq],
                "text": event_text(event),
            }
            for event in loaded["events"]
        ]

    async def filter_sessions(self, filters: Any) -> list[dict]:
        clauses = materialise_session_filters(filters)
        return apply_session_filters(await self.list_sessions(), clauses)

    async def filter_session_events(self, session_id: str, filters: Any) -> list[dict]:
        clauses = materialise_event_filters(filters)
        return apply_event_filters(await self.list_events(session_id), clauses)


__all__ = [
    "SessionQueryEngine",
    "SessionCorpus",
    "classify_surface",
    "event_text",
    "message_text",
]
