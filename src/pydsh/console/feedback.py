"""``/feedback`` — what a person thinks of this conversation.

The event is **log-only**, and that is the entire design. Feedback about a
conversation must not become part of it: on the surface, the model would read
the verdict on its last answer as input to the next one, and the conversation
the feedback was about would stop existing. The same reasoning keeps message
feedback in a sidecar; this one is about the session as a whole, so it belongs
on the log, off the surface.

`record_feedback` is separate from the command because the command is only one
way to trigger it. A client with a thumbs-up button records the same event.
"""

from __future__ import annotations

from typing import Any, Optional

from plugkit import Service

from ..operating.commands import CommandResult

#: The event type. Log-only — see the module docstring.
FEEDBACK_EVENT = "feedback/record"

USAGE = "Usage: /feedback <text>"

#: What each sharing policy means to the person typing. Disclosed rather than
#: assumed: whether their words leave this machine is theirs to know before
#: they type them, not after.
DISCLOSURES = {
    "full": "Session sharing is enabled.",
    "feedback-only": (
        "Session sharing is feedback-gated; recording feedback releases this "
        "session's prefix for sharing."
    ),
    "disabled": "Session sharing is disabled.",
}

UNCONFIGURED = "Session sharing is not configured."


class SessionFeedbackError(ValueError):
    """Session feedback that cannot be recorded as given.

    Named apart from :class:`pydsh.sidecar.FeedbackError`, which is about a
    rating on one *message*. Two things called ``FeedbackError`` in one
    package is a coin flip at every import site.
    """


def record_feedback(session: Any, text: str) -> Any:
    """Append one piece of feedback, however it was triggered.

    :raises SessionFeedbackError: the text is empty once trimmed — validated before
        the append, so a refused call leaves no event behind.
    """
    normalized = (text or "").strip()
    if not normalized:
        raise SessionFeedbackError("feedback text must not be empty")
    return session.append(FEEDBACK_EVENT, {"text": normalized})


def sharing_disclosure(telemetry: Any) -> str:
    """What to tell the person about where this goes."""
    if telemetry is None:
        return UNCONFIGURED
    return DISCLOSURES.get(getattr(telemetry, "sharing", ""), DISCLOSURES["disabled"])


class FeedbackCommand(Service):
    """Registers ``/feedback``."""

    provide = "command_feedback"
    inject = ["commands"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._root = getattr(ctx, "root", ctx)
        dispose = ctx.commands.register(
            "feedback", "Record feedback about this session", self._run
        )
        ctx.effect(lambda: dispose)

    def _anonymous_id(self) -> Optional[str]:
        identity = getattr(self._root, "anonymous_user_id", None)
        return getattr(identity, "value", None) if identity is not None else None

    def _run(self, invocation: Any) -> CommandResult:
        session = getattr(invocation.agent, "session", None)
        if session is None:
            return CommandResult.error(f"/feedback needs a session. {USAGE}")

        try:
            record_feedback(session, invocation.raw_input)
        except SessionFeedbackError:
            return CommandResult.error(f"Feedback text is required. {USAGE}")

        telemetry = getattr(self._root, "session_telemetry", None)
        lines = [f"Feedback recorded for session {session.header.id}."]
        anonymous = self._anonymous_id()
        if anonymous:
            lines.append(f"Anonymous user: {anonymous}.")
        lines.append(sharing_disclosure(telemetry))
        return CommandResult.success(" ".join(lines))


__all__ = [
    "FeedbackCommand",
    "SessionFeedbackError",
    "record_feedback",
    "sharing_disclosure",
    "FEEDBACK_EVENT",
    "DISCLOSURES",
    "UNCONFIGURED",
    "USAGE",
]
