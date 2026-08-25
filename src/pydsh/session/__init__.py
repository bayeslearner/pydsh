"""The session seam: the event-sourced log, its store, and persistence.

- ``Session`` — the append-only event log and derived messages (a plain class).
- ``SessionStore`` — the ``ctx.sessions`` service holding live sessions.
- ``SqliteSessionPersistence`` — the durable SQLite backend.
- ``SessionProjections`` — reading the log as state, the fold primitive.
- ``SessionStats`` — the first unit: turns, steps, and four timings.
- ``CheckpointPolicy`` — periodic durability, so a flush is not something a
  consumer has to remember.

`ponytail:` ``derive_messages`` returns payload messages directly; the rich
typed ``Message``/ContentBlock vocabulary arrives with the llm seam. The
``surface_op: replace`` path (compaction) is defined but not exercised until a
compaction sprint.
"""

from .cache import CACHE_DOMAIN, ProjectionCache
from .checkpoint import DEFAULT_EVERY_TURNS, CheckpointPolicy
from .events import (
    EVENT_DATA_FIELDS,
    EVENT_TYPES,
    SESSION_FORMAT_VERSION,
    STEP_EVENTS,
    SURFACE_EVENTS,
    TURN_EVENTS,
)
from .pairing import balanced_after, balanced_before, surface_balance
from .projection import (
    EMPTY_WATERMARK,
    FIRST_SEQ,
    ProjectionDefinition,
    SessionProjections,
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
from .stats import SESSION_STATS_KEY, SessionStats, session_stats_definition
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
    # projections
    "SessionProjections",
    "ProjectionDefinition",
    "SessionStats",
    "session_stats_definition",
    "SESSION_STATS_KEY",
    "EMPTY_WATERMARK",
    "FIRST_SEQ",
    # surface pairing
    "surface_balance",
    "balanced_before",
    "balanced_after",
    # durability
    "ProjectionCache",
    "CACHE_DOMAIN",
    "CheckpointPolicy",
    "DEFAULT_EVERY_TURNS",
    # vocabulary
    "SESSION_FORMAT_VERSION",
    "EVENT_TYPES",
    "EVENT_DATA_FIELDS",
    "SURFACE_EVENTS",
    "TURN_EVENTS",
    "STEP_EVENTS",
]
