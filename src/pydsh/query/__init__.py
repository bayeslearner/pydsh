"""Reading history — searching a corpus of sessions, and pointing at one.

Read-only throughout: nothing here writes, so nothing here can corrupt a log.

- ``ctx.session_query`` — list, read, and filter sessions and their events.
  Filters are *data*, so a client can send them over a wire; text search is
  literal, because compiling a searcher's text is an injection.
- ``ctx.session_references`` — canonical URIs, Markdown mentions, and a bounded
  projection of a referenced conversation.
"""

from .engine import (
    SessionCorpus,
    SessionQueryEngine,
    classify_surface,
    event_text,
    message_text,
)
from .filters import (
    AVAILABILITY,
    EVENT_FILTER_KINDS,
    SESSION_FILTER_KINDS,
    SURFACE_CLASSES,
    QueryError,
    apply_event_filters,
    apply_session_filters,
    compile_text_filter,
    materialise_event_filters,
    materialise_session_filters,
)
from .reference import (
    DEFAULT_MAX_REFERENCE_BYTES,
    DEFAULT_MAX_REFERENCES,
    SCHEME,
    SessionReferenceError,
    SessionReferences,
    decode_reference_uri,
    encode_reference_uri,
    format_mention,
    parse_references,
    tag_safe_json,
)

__all__ = [
    "SessionQueryEngine",
    "SessionCorpus",
    "classify_surface",
    "event_text",
    "message_text",
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
    "SessionReferences",
    "SessionReferenceError",
    "encode_reference_uri",
    "decode_reference_uri",
    "format_mention",
    "parse_references",
    "tag_safe_json",
    "SCHEME",
    "DEFAULT_MAX_REFERENCES",
    "DEFAULT_MAX_REFERENCE_BYTES",
]
