"""Persisting projection checkpoints — a shortcut, never authority.

Spec 05 built the cold-read ladder with nothing to persist the rows. This is
that store, and it turns "list a hundred archived conversations with their turn
counts" from a hundred full log loads into one table read.

Everything here is derived. Delete the whole cache and every value comes back
by folding the logs; when a row and the log disagree, the log wins. Two rules
keep that true:

**The cache may lag the log; it must never lead it.** A checkpoint is taken,
then the *log* is flushed, then the rows are written. A crash between the last
two leaves the cache behind — the next cold read replays a longer tail and is
still right. Reverse the order and a crash leaves rows folded from events the
log does not contain: values for a conversation that never happened, with
nothing to reveal it.

**Rows from another lifetime are discarded whole.** The stored header is an
identity witness. An id reused after a rebuild, or a store swapped underneath,
produces rows that look perfectly well-formed and describe a different
conversation — so they are thrown away rather than merged.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from plugkit import Service

from ..storage import define_domain, domain_table
from .projection import FIRST_SEQ

logger = logging.getLogger("pydsh.session.cache")

#: Events between throttled writes, between the forced points.
DEFAULT_WRITE_EVERY_EVENTS = 10

#: The event that forces a write however few have accumulated.
FORCED_EVENT = "turn/end"

#: One row per session: the identity witness plus the checkpoint rows.
CACHE_DOMAIN = define_domain(
    "projection_cache",
    version=1,
    tables={"sessions": domain_table()},
)


def _identity_of(header: Any) -> dict:
    """What ties a row to one session's lifetime.

    Not the id alone: an id can be reused after a rebuild, and the rows from
    the previous life would look entirely valid. The creation time makes two
    sessions that share a name distinguishable.
    """
    return {
        "id": getattr(header, "id", None),
        "created_at": getattr(header, "created_at", None),
        "version": getattr(header, "version", None),
    }


class ProjectionCache(Service):
    """Provides ``ctx.projection_cache``."""

    provide = "projection_cache"
    inject = ["sessions", "session_projections", "storage_domain"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        self._write_every = int(
            config.get("write_every_events", DEFAULT_WRITE_EVERY_EVENTS)
        )
        self._projections = ctx.session_projections
        self._sessions = ctx.sessions
        self._domain: Any = None
        self._pending: dict[str, int] = {}
        # Per instance, not per class: a class-level set would be shared by
        # every cache in the process, and a test's drain would wait on another
        # context's writes.
        self._inflight: set = set()
        ctx.on("session/event", self._on_event)

    # -- the store --------------------------------------------------------- #
    async def start(self) -> None:
        """Open the cache's storage domain. Idempotent."""
        if self._domain is None:
            self._domain = await self.ctx.storage_domain.open(CACHE_DOMAIN)

    def _table(self) -> Any:
        if self._domain is None:
            raise RuntimeError(
                "the projection cache has not been started: await "
                "ctx.projection_cache.start() before using it"
            )
        return self._domain.table("sessions")

    # -- reading ----------------------------------------------------------- #
    def cached_snapshot(self, header: Any) -> Optional[dict]:
        """Values from stored rows, touching no log at all.

        The header the caller already holds is the identity witness, so a row
        from another lifetime never reaches a reader.

        ``as_of_seq`` is the *lowest* watermark among the values served. Under
        higher-seq-wins merging, under-reporting is safe and over-reporting
        would let a stale value beat a fresher push.
        """
        record = self._record_for(header)
        if record is None:
            return None
        values = self._projections.view_checkpoint(record["rows"])
        if not values:
            return None
        as_of = min(record["rows"][key]["seq"] for key in values)
        return {"as_of_seq": as_of, "values": values}

    async def cold_snapshot(self, session_id: str) -> dict:
        """Current values for a persisted session, replaying only what is needed."""
        backend = self._persistence()
        record = self._table().get(session_id)
        cached = record["rows"] if record is not None else {}

        floor = self._projections.restore_floor(cached)
        if floor is None:
            # No units registered: there is nothing to fold, but the caller
            # still asked about a specific session, so prove it exists rather
            # than answering for one that was never persisted.
            probe = await backend.read_from(session_id, FIRST_SEQ)
            if probe is None:
                raise LookupError(f"no persisted session {session_id!r}")
            events = probe["events"]
            return {"as_of_seq": events[-1].seq if events else 0, "values": {}}

        tail = await backend.read_from(session_id, floor)
        if tail is None:
            raise LookupError(f"no persisted session {session_id!r}")

        related = record is None or record["identity"] == _identity_of(tail["meta"])
        restored = None
        if related:
            try:
                restored = self._projections.restore(cached, tail["events"], floor)
            except Exception as exc:  # noqa: BLE001 - recoverable: re-read below
                logger.info(
                    "projection cache: rows for %r unusable (%s); re-reading the log",
                    session_id,
                    exc,
                )
        if restored is None:
            # An unrelated identity or a refused restore both mean the rows
            # cannot be trusted. Reading the whole log and folding from `init`
            # is slower and correct; trusting them is fast and wrong.
            whole = await backend.read_from(session_id, FIRST_SEQ)
            if whole is None:
                raise LookupError(f"no persisted session {session_id!r}")
            restored = self._projections.restore({}, whole["events"], FIRST_SEQ)
            tail = whole

        await self._put_soft(
            session_id, _identity_of(tail["meta"]), restored["checkpoint"], "cold read"
        )
        return restored["snapshot"]

    # -- writing ----------------------------------------------------------- #
    async def write(self, session: Any) -> None:
        """A forced durable checkpoint. Propagates; callers on the soft path wrap it.

        The order is the whole invariant: the slice is taken first, the *log*
        reaches disk second, the rows third. A crash after the log write leaves
        the cache behind it, which the next cold read repairs by replaying more.
        """
        rows = self._projections.checkpoint(session)
        self._pending.pop(session.id, None)
        if self._sessions.get(session.id) is session:
            await self._sessions.flush(session)
        await self._table().put(
            session.id, {"identity": _identity_of(session.header), "rows": rows}
        )

    def _on_event(self, session: Any, event: Any) -> None:
        """Count events, and schedule a write at the forced point or the bound."""
        if event.type == FORCED_EVENT:
            self._schedule(session, FORCED_EVENT)
            return
        count = self._pending.get(session.id, 0) + 1
        self._pending[session.id] = count
        if count >= self._write_every:
            self._schedule(session, "event count")

    def _schedule(self, session: Any, trigger: str) -> None:
        """Start a fail-soft checkpoint without making the append wait.

        ``session/event`` is a synchronous post-commit broadcast, so the write
        cannot be awaited here.
        """
        if self._domain is None:
            return  # not started: nothing to write to yet
        try:
            task = asyncio.ensure_future(self._write_soft(session, trigger))
        except RuntimeError:
            return  # no running loop; the next checkpoint picks it up
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _write_soft(self, session: Any, trigger: str) -> None:
        """A checkpoint that never becomes the caller's problem (I3)."""
        try:
            await self.write(session)
        except Exception as exc:  # noqa: BLE001 - stale beats broken
            logger.warning(
                "projection cache: %s checkpoint failed: %s", trigger, exc, exc_info=exc
            )

    async def _put_soft(
        self, session_id: str, identity: dict, rows: dict, what: str
    ) -> None:
        try:
            await self._table().put(session_id, {"identity": identity, "rows": rows})
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "projection cache: %s write-back failed: %s", what, exc, exc_info=exc
            )

    async def drain(self) -> None:
        """Wait for any checkpoint in flight — for tests and for shutdown."""
        while self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)

    # -- internals --------------------------------------------------------- #
    def _record_for(self, header: Any) -> Optional[dict]:
        record = self._table().get(getattr(header, "id", None))
        if record is None:
            return None
        if record["identity"] != _identity_of(header):
            return None  # another lifetime (I2)
        return record

    def _persistence(self) -> Any:
        backend = self._sessions.persistence
        if backend is None:
            raise RuntimeError(
                "no persistence backend is attached, so there is no log to cold-read"
            )
        return backend


__all__ = [
    "ProjectionCache",
    "CACHE_DOMAIN",
    "DEFAULT_WRITE_EVERY_EVENTS",
    "FORCED_EVENT",
]
