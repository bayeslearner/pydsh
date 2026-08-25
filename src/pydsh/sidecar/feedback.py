"""``ctx.message_feedback`` — an opinion about a message, held beside the log.

Deliberately **not** a session event, which is the one design decision worth
the module. An event would be on the *surface*, so the model would read the
user's rating of its own previous answer as part of the conversation — which
changes the conversation the rating was about. An opinion belongs next to the
log, not in it.

Two protections, both borrowed from patterns already established here. Rows are
fenced by the session's *lifetime identity* rather than its id, so a reused id
does not surface a previous life's ratings. And writes are compare-and-set
against a version token, so two clients editing one note do not silently
overwrite each other — the same reasoning as goals.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Optional

from plugkit import Service

from ..storage import define_domain, domain_table

#: Bytes a note may reach. Enough for a paragraph of reasoning; not enough to
#: make the sidecar a second conversation.
DEFAULT_MAX_NOTE_BYTES = 4_096

#: What a rating may be.
RATINGS = ("up", "down", None)

#: One row per session, holding that session's message feedback.
FEEDBACK_DOMAIN = define_domain(
    "message_feedback",
    version=1,
    tables={"sessions": domain_table()},
)


class FeedbackError(ValueError):
    """A refusal, with a code a client routes on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def lifetime_identity(session: Any) -> dict:
    """What ties feedback to *this* life of a session.

    Not the id alone: an id can be reused after a rebuild, and the previous
    life's ratings would look perfectly valid attached to a new conversation.
    """
    header = getattr(session, "header", None)
    return {
        "id": getattr(header, "id", None),
        "created_at": getattr(header, "created_at", None),
        "cwd": getattr(header, "cwd", None),
    }


def _check_note(note: Any) -> Optional[str]:
    if note is None:
        return None
    if not isinstance(note, str) or not note.strip():
        raise FeedbackError("note-blank", "a feedback note must not be blank")
    if len(note.encode("utf-8")) > DEFAULT_MAX_NOTE_BYTES:
        raise FeedbackError(
            "note-too-large",
            f"a feedback note may be at most {DEFAULT_MAX_NOTE_BYTES} bytes",
        )
    return note


def _check_rating(rating: Any) -> Optional[str]:
    if rating not in RATINGS:
        raise FeedbackError(
            "rating-invalid",
            f"rating {rating!r} is unknown; expected up, down, or nothing",
        )
    return rating


class MessageFeedback(Service):
    """Provides ``ctx.message_feedback``."""

    provide = "message_feedback"
    inject = ["storage_domain"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._domain: Any = None
        # One write chain per session, so two clients rating different messages
        # in one conversation cannot interleave a read-compare-write.
        self._locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        """Open the storage domain. Idempotent."""
        if self._domain is None:
            self._domain = await self.ctx.storage_domain.open(FEEDBACK_DOMAIN)

    def _table(self) -> Any:
        if self._domain is None:
            raise RuntimeError(
                "message feedback has not been started: await "
                "ctx.message_feedback.start() first"
            )
        return self._domain.table("sessions")

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    def _row(self, session: Any) -> dict:
        """This session's row, or an empty one if the fence does not match."""
        stored = self._table().get(session.id)
        if stored is None or stored.get("identity") != lifetime_identity(session):
            return {"identity": lifetime_identity(session), "entries": {}}
        return stored

    async def list(self, session: Any) -> dict:
        """Every rating on this session's messages, fenced by its lifetime."""
        await self.start()
        return dict(self._row(session)["entries"])

    async def get(self, session: Any, message_id: str) -> Optional[dict]:
        await self.start()
        return self._row(session)["entries"].get(message_id)

    async def put(
        self,
        session: Any,
        message_id: str,
        rating: Optional[str] = None,
        note: Optional[str] = None,
        version: Optional[str] = None,
    ) -> dict:
        """Replace a message's feedback, against a version token (I4).

        :param version: the token from the entry being replaced, or ``None``
            when creating one.
        :raises FeedbackError: the stored entry has moved on.
        """
        await self.start()
        checked = {"rating": _check_rating(rating), "note": _check_note(note)}

        async with self._lock(session.id):
            row = self._row(session)
            existing = row["entries"].get(message_id)
            stored_version = existing.get("version") if existing else None

            if version != stored_version:
                raise FeedbackError(
                    "version-mismatch",
                    f"feedback for {message_id!r} is at version {stored_version!r}, "
                    f"not {version!r} — re-read and try again",
                )

            if existing and existing["rating"] == checked["rating"] and existing["note"] == checked["note"]:
                # A no-op write must not churn the version: another client
                # holding the same token would be invalidated for nothing.
                return existing

            entry = {**checked, "version": uuid.uuid4().hex[:12], "message_id": message_id}
            await self._table().put(
                session.id, {**row, "entries": {**row["entries"], message_id: entry}}
            )
            return entry

    async def delete(
        self, session: Any, message_id: str, version: Optional[str] = None
    ) -> bool:
        """Remove feedback. Absent is success; present is version-checked."""
        await self.start()
        async with self._lock(session.id):
            row = self._row(session)
            existing = row["entries"].get(message_id)
            if existing is None:
                return True  # idempotent: the caller wanted it gone, and it is
            if version != existing.get("version"):
                raise FeedbackError(
                    "version-mismatch",
                    f"feedback for {message_id!r} is at version "
                    f"{existing.get('version')!r}, not {version!r}",
                )
            entries = {k: v for k, v in row["entries"].items() if k != message_id}
            await self._table().put(session.id, {**row, "entries": entries})
            return True


__all__ = [
    "MessageFeedback",
    "FeedbackError",
    "FEEDBACK_DOMAIN",
    "lifetime_identity",
    "DEFAULT_MAX_NOTE_BYTES",
    "RATINGS",
]
