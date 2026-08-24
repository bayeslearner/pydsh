"""The session seam: the event-sourced log, its store, and persistence.

- ``Session`` — the append-only event log and derived messages (a plain class).
- ``SessionStore`` — the ``ctx.sessions`` service holding live sessions.
- ``SqliteSessionPersistence`` — the durable SQLite backend.

`ponytail:` ``derive_messages`` returns payload messages directly; the rich
typed ``Message``/ContentBlock vocabulary arrives with the llm seam. The
``surface_op: replace`` path (compaction) is defined but not exercised until a
compaction sprint.
"""

from .events import (
    EVENT_DATA_FIELDS,
    EVENT_TYPES,
    SESSION_FORMAT_VERSION,
    STEP_EVENTS,
    SURFACE_EVENTS,
    TURN_EVENTS,
)
from .persistence import (
    SessionFormatUnsupportedError,
    SessionPersistence,
    SessionPersistenceError,
    SqliteSessionPersistence,
)
from .session import (
    InvalidEventData,
    Session,
    SessionError,
    SessionEvent,
    SessionHeader,
    UnknownEventType,
)
from .store import SessionStore

__all__ = [
    "Session",
    "SessionEvent",
    "SessionHeader",
    "SessionStore",
    "SessionError",
    "InvalidEventData",
    "UnknownEventType",
    "SessionPersistence",
    "SessionPersistenceError",
    "SessionFormatUnsupportedError",
    "SqliteSessionPersistence",
    # vocabulary
    "SESSION_FORMAT_VERSION",
    "EVENT_TYPES",
    "EVENT_DATA_FIELDS",
    "SURFACE_EVENTS",
    "TURN_EVENTS",
    "STEP_EVENTS",
]
