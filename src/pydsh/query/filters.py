"""Filters as data — validated, copied, applied.

Filters arrive from a client over a wire as JSON, which is why they are values
rather than callbacks. They are checked before use, so a client sending a
filter this service does not understand is *told*, rather than silently handed
everything.

The text filter is the one with a sharp edge. It matches **literally**: the
search text is escaped and only runs of whitespace are made flexible. Compiling
a searcher's text as a pattern is one line and more powerful, and it is an
injection — `(a+)+b` typed into a search box is a denial of service, and
`a.*b` quietly returns things nobody asked about.
"""

from __future__ import annotations

import re
from typing import Any, Optional

#: Where a session can be read from.
AVAILABILITY = ("live", "persisted")

#: Where an event sits relative to the model's view. ``shadowed`` only became
#: possible with compaction: an event that would be on the surface, replaced by
#: a summary. "What can the model see" and "what ever happened" are different
#: questions from that point on.
SURFACE_CLASSES = ("current", "shadowed", "log-only")

SESSION_FILTER_KINDS = ("id", "cwd", "availability", "created-at")
EVENT_FILTER_KINDS = ("seq", "time", "type", "surface", "text")


class QueryError(ValueError):
    """A malformed query, with a code a client can match on."""

    def __init__(self, message: str, code: str = "SESSION_QUERY_INVALID_FILTER") -> None:
        super().__init__(message)
        self.code = code


def compile_text_filter(text: str) -> re.Pattern:
    """A literal, case-insensitive, whitespace-flexible matcher.

    Whitespace flexibility is the one ergonomic concession: a phrase that
    wrapped across lines in the transcript still matches when typed on one.
    Everything else is escaped.
    """
    trimmed = text.strip()
    if not trimmed:
        # Matching everything is never what a search meant, and returning the
        # whole corpus for an accidentally empty box is worse than an error.
        raise QueryError("a text filter needs non-whitespace text")
    pattern = r"\s+".join(re.escape(part) for part in trimmed.split())
    return re.compile(pattern, re.IGNORECASE)


def _copy_range(kind: str, raw: dict) -> dict:
    """A validated ``from``/``to`` pair, either bound optional."""
    out: dict = {}
    for edge in ("from", "to"):
        value = raw.get(edge)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise QueryError(f"{kind} filter {edge} must be a number, got {value!r}")
        out[edge] = value
    if "from" in out and "to" in out and out["from"] > out["to"]:
        raise QueryError(
            f"{kind} filter is inverted: from {out['from']} is after to {out['to']}"
        )
    return out


def _string_values(raw: dict, kind: str, allow_none: bool = False) -> list:
    values = raw.get("values")
    if not isinstance(values, list):
        raise QueryError(f"{kind} filter values must be a list")
    for value in values:
        if value is None and allow_none:
            continue
        if not isinstance(value, str):
            raise QueryError(f"{kind} filter values must be strings, got {value!r}")
    return list(values)


def materialise_session_filters(filters: Any) -> list[dict]:
    """Validate and copy session-level filters."""
    if not isinstance(filters, list):
        raise QueryError("filters must be a list")
    out: list[dict] = []
    for raw in filters:
        if not isinstance(raw, dict):
            raise QueryError("each filter must be an object")
        kind = raw.get("kind")
        if kind in ("id", "cwd"):
            out.append({"kind": kind, "values": _string_values(raw, kind, allow_none=kind == "cwd")})
        elif kind == "availability":
            values = _string_values(raw, kind)
            for value in values:
                if value not in AVAILABILITY:
                    raise QueryError(
                        f"availability filter value {value!r} is unknown; expected "
                        f"one of {', '.join(AVAILABILITY)}"
                    )
            out.append({"kind": kind, "values": values})
        elif kind == "created-at":
            out.append({"kind": kind, **_copy_range(kind, raw)})
        else:
            raise QueryError(
                f"unknown session filter kind {kind!r}; expected one of "
                f"{', '.join(SESSION_FILTER_KINDS)}"
            )
    return out


def materialise_event_filters(filters: Any) -> list[dict]:
    """Validate and copy event-level filters."""
    if not isinstance(filters, list):
        raise QueryError("filters must be a list")
    out: list[dict] = []
    for raw in filters:
        if not isinstance(raw, dict):
            raise QueryError("each filter must be an object")
        kind = raw.get("kind")
        if kind in ("seq", "time"):
            out.append({"kind": kind, **_copy_range(kind, raw)})
        elif kind == "type":
            out.append({"kind": kind, "values": _string_values(raw, kind)})
        elif kind == "surface":
            values = _string_values(raw, kind)
            for value in values:
                if value not in SURFACE_CLASSES:
                    raise QueryError(
                        f"surface filter value {value!r} is unknown; expected one "
                        f"of {', '.join(SURFACE_CLASSES)}"
                    )
            out.append({"kind": kind, "values": values})
        elif kind == "text":
            text = raw.get("text")
            if not isinstance(text, str):
                raise QueryError("text filter text must be a string")
            out.append({"kind": kind, "text": text, "pattern": compile_text_filter(text)})
        else:
            raise QueryError(
                f"unknown event filter kind {kind!r}; expected one of "
                f"{', '.join(EVENT_FILTER_KINDS)}"
            )
    return out


def _in_range(value: Any, bounds: dict) -> bool:
    if value is None:
        return False
    low, high = bounds.get("from"), bounds.get("to")
    return (low is None or value >= low) and (high is None or value <= high)


def apply_session_filters(records: list[dict], filters: list[dict]) -> list[dict]:
    """AND across clauses, OR within a clause's values (I4)."""
    out = list(records)
    for clause in filters:
        kind = clause["kind"]
        if kind == "id":
            wanted = set(clause["values"])
            out = [r for r in out if r["id"] in wanted]
        elif kind == "cwd":
            wanted = set(clause["values"])
            out = [r for r in out if r.get("cwd") in wanted]
        elif kind == "availability":
            wanted = set(clause["values"])
            out = [r for r in out if wanted & set(r.get("availability", ()))]
        elif kind == "created-at":
            out = [r for r in out if _in_range(r.get("created_at"), clause)]
    return out


def apply_event_filters(documents: list[dict], filters: list[dict]) -> list[dict]:
    """The same composition, over event documents."""
    out = list(documents)
    for clause in filters:
        kind = clause["kind"]
        if kind == "seq":
            out = [d for d in out if _in_range(d["seq"], clause)]
        elif kind == "time":
            out = [d for d in out if _in_range(d["time"], clause)]
        elif kind == "type":
            wanted = set(clause["values"])
            out = [d for d in out if d["type"] in wanted]
        elif kind == "surface":
            wanted = set(clause["values"])
            out = [d for d in out if d["surface"] in wanted]
        elif kind == "text":
            pattern = clause["pattern"]
            out = [d for d in out if pattern.search(d["text"])]
    return out


__all__ = [
    "QueryError",
    "compile_text_filter",
    "materialise_session_filters",
    "materialise_event_filters",
    "apply_session_filters",
    "apply_event_filters",
    "AVAILABILITY",
    "SURFACE_CLASSES",
    "SESSION_FILTER_KINDS",
    "EVENT_FILTER_KINDS",
]
