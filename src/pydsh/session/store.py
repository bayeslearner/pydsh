"""``SessionStore`` — the ``ctx.sessions`` service.

Holds the live sessions, gives them out by id, publishes their growth on
``session/event``, and owns the persistence backend. A session is created and
bound to the calling fiber, so a fiber that unloads disposes its sessions with
it.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from plugkit import Service

from .events import SESSION_FORMAT_VERSION
from .session import Session, SessionError, SessionHeader


class SessionStore(Service):
    """The ``ctx.sessions`` service."""

    provide = "sessions"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._sessions: dict[str, Session] = {}
        self._persistence: Any = None
        # The durability checkpoint is a listener-driven fan-out, matching the
        # reference: the backend subscribes to `session/flush` (parallel) and
        # the store's `flush()` dispatches it and awaits the listeners.
        self.ctx.on("session/flush", self._on_flush)

    # -- durability -------------------------------------------------------

    def attach_persistence(self, backend: Any) -> None:
        """Attach a persistence backend; replaces any prior one."""
        self._persistence = backend

    def has_persistence(self) -> bool:
        return self._persistence is not None

    @property
    def persistence(self) -> Any:
        """The attached backend, or ``None``.

        Public because the cold-read path needs the backend directly — it reads
        a *tail* of a log rather than a whole session, which is not something
        the store itself does.
        """
        return self._persistence

    async def flush(self, session: Session) -> None:
        """Await the durability checkpoint for one session.

        Every ``session/flush`` listener runs (parallel) and the caller awaits
        all of them, so an acknowledged flush is on disk. With no listeners it
        returns immediately.
        """
        await self.ctx.parallel("session/flush", session)

    async def _on_flush(self, session: Session) -> None:
        if self._persistence is not None:
            await self._persistence.flush(session)

    # -- lifecycle --------------------------------------------------------

    def create(
        self,
        id: Optional[str] = None,
        *,
        cwd: str | None = None,
        meta: Optional[dict] = None,
    ) -> Session:
        """Create and register a live session, bound to the calling fiber."""
        session_id = id or f"session-{len(self._sessions) + 1}"
        if session_id in self._sessions:
            raise SessionError(f"session {session_id!r} already exists")
        ts = meta.get("created_at", 0.0) if meta else 0.0
        header = SessionHeader(
            version=SESSION_FORMAT_VERSION,
            id=session_id,
            created_at=ts,
            cwd=cwd,
        )
        session = Session(self.ctx, id=session_id, header=header)
        self._sessions[session_id] = session

        # Bind the session's removal to the creating fiber when one is active
        # (plugin-created sessions dispose with their fiber). Outside a plugin
        # apply — a root-context create, as in the later agent loop — there is
        # no active fiber, so the session is store-owned until the store
        # unloads.
        fiber = self.ctx.fiber
        # The root context (not inside a plugin apply) has no runtime; an
        # effect there disposes immediately, so only fiber-bind when a plugin
        # fiber is live. Otherwise the session is store-owned until unload.
        if fiber.runtime is not None:
            def _install() -> Callable:
                # `ctx.effect` runs `_install` now and keeps its *return value*
                # as the disposer, invoked when the fiber unloads.
                return lambda: self._sessions.pop(session_id, None)

            self.ctx.effect(_install, f"session dispose {session_id}")
        return session

    async def resume(self, id: str) -> Session:
        """Bring a persisted session back as a live one.

        A session already live is returned as it is — reloading it from disk
        would discard whatever has been appended since the last flush, which is
        data loss dressed up as a refresh.

        The loaded session is rebound to this store's context. The backend
        rebuilds it without one (it has no context to give), and a session with
        no context cannot broadcast ``session/event``.
        """
        live = self._sessions.get(id)
        if live is not None:
            return live
        if self._persistence is None:
            raise SessionError(
                f"cannot resume session {id!r}: no persistence backend is "
                "attached; call sessions.attach_persistence(...) first"
            )
        session = await self._persistence.load(id)
        if session is None:
            raise SessionError(f"no persisted session {id!r} to resume")
        session.ctx = self.ctx
        self._sessions[id] = session
        return session

    def get(self, id: str) -> Optional[Session]:
        return self._sessions.get(id)

    def list(self) -> list[Session]:
        return list(self._sessions.values())


__all__ = ["SessionStore", "SessionError"]
