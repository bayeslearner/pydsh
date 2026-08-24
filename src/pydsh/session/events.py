"""The session event vocabulary — the reference's ``SessionEventMap``.

Defines the core append-only event types, which are surface (model-visible)
as opposed to log-only, and the on-disk format version. This module imports
nothing from the kernel; it is pure vocabulary any seam can read.

The names mirror the TypeScript reference (``core/session``) so dsh's own
documentation describes this module accurately, and a later conformance suite
can assert against the TS source the same way plugkit's does.
"""

from __future__ import annotations

# On-disk session-log format version. Stamped into every header; the
# persistence backend refuses any other version on load. Pre-release, so no
# migration: incompatible logs are rejected, never silently skipped.
SESSION_FORMAT_VERSION = 0

# The events whose payloads are model-visible messages. Only these may carry
# surface metadata (surface_op / source_event_seqs) and enter the derived
# message list.
SURFACE_EVENTS = ("user/message", "assistant/message", "tool/result")

# Boundaries of model execution. Log-only: recorded, never surfaced.
TURN_EVENTS = ("turn/start", "turn/end")
STEP_EVENTS = ("step/start", "step/end")

# Every event a consumer may append via `session.append(type, data)`. Surface
# events are the message carriers; the rest are log-only.
EVENT_TYPES = (
    *TURN_EVENTS,
    *STEP_EVENTS,
    # user/message    — a user-role message on the surface
    # assistant/chunk — raw stream chunk (token-level replay fidelity)
    "assistant/chunk",
    # assistant/message — assembled assistant reply + optional usage
    "assistant/message",
    # tool/call — the model requested one tool: name + raw arguments JSON
    "tool/call",
    # tool/result — a completed tool call's model-facing result
    "tool/result",
)

# The payload shape of each event type, as the type key -> field names. Kept
# as documentation plus a cheap guard: `session.append` validates the keys.
EVENT_DATA_FIELDS: dict[str, tuple[str, ...]] = {
    "turn/start": ("turn",),
    "turn/end": ("turn", "reason"),
    "step/start": ("turn", "step"),
    "step/end": ("turn", "step"),
    "user/message": ("content", "role", "source"),
    "assistant/chunk": ("turn", "step", "chunk"),
    "assistant/message": ("turn", "step", "message", "usage"),
    "tool/call": ("turn", "step", "callId", "name", "arguments"),
    "tool/result": ("turn", "step", "message", "error", "meta"),
}
