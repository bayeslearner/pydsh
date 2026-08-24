"""Cancellation as a scope several layers can share.

The reference uses the web platform's ``AbortSignal``: one object handed to the
agent loop, the LLM adapter, and any plugin that wants to notice, so a single
"stop" reaches all of them. Python has no such primitive, and plugkit's
``Signal`` is a *reactive value* with subscribers — a different thing that
happens to share the name. So the semantics are ported here, in a module that
imports nothing.

The shape:

- :meth:`CancelSignal.abort` cancels, once, carrying a reason.
- :meth:`CancelSignal.throw_if_aborted` is the checkpoint an async loop calls.
- :meth:`CancelSignal.any` fuses sources, mirroring ``AbortSignal.any``.

Fused signals are **disposable**, which the reference's are not. Fusing
registers a listener on every source, and the agent loop fuses a long-lived
teardown signal once per agent it resumes; without a way to detach, those
listeners accumulate for the life of the process.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("pydsh.cancel")


class CancelledError(Exception):
    """Raised at a checkpoint when the operation's signal has been aborted."""

    def __init__(self, reason: Any = None) -> None:
        super().__init__(reason)
        self.reason = reason


class CancelSignal:
    """One cancellable scope."""

    def __init__(self) -> None:
        self._aborted = False
        self._reason: Any = None
        self._listeners: list[Callable[[], None]] = []
        # Detachers for the sources this signal was fused from, if any.
        self._detachers: list[Callable[[], bool]] = []

    # -- state ------------------------------------------------------------- #
    @property
    def aborted(self) -> bool:
        """Whether this scope has been cancelled."""
        return self._aborted

    @property
    def reason(self) -> Any:
        """Why it was cancelled — ``None`` while it is live."""
        return self._reason

    # -- cancelling -------------------------------------------------------- #
    def abort(self, reason: Any = None) -> None:
        """Cancel this scope, idempotently, notifying listeners synchronously.

        A listener that raises is contained: cancellation has already happened
        by the time listeners run, so one bad observer must not leave the
        signal half-aborted or stop the others from hearing about it.
        """
        if self._aborted:
            return
        self._aborted = True
        self._reason = reason
        for listener in list(self._listeners):
            try:
                listener()
            except Exception as exc:  # noqa: BLE001 - containment is the point
                logger.warning("cancel listener failed: %s", exc, exc_info=exc)

    def throw_if_aborted(self) -> None:
        """The checkpoint: raise :class:`CancelledError` if already cancelled."""
        if self._aborted:
            raise CancelledError(self._reason)

    # -- listening --------------------------------------------------------- #
    def add_listener(self, callback: Callable[[], None]) -> Callable[[], bool]:
        """Register a cancellation listener; returns a detach callable."""
        self._listeners.append(callback)

        def detach() -> bool:
            try:
                self._listeners.remove(callback)
            except ValueError:
                return False
            return True

        return detach

    # -- fusion ------------------------------------------------------------ #
    @staticmethod
    def any(signals: list[Optional["CancelSignal"]]) -> "CancelSignal":
        """Fuse sources: whichever cancels first cancels the result.

        ``None`` entries are skipped, so a caller can pass an optional signal
        without branching. A source that is *already* aborted cancels the fused
        signal immediately, and the rest are still attached — the fused signal
        is inert once aborted, and attaching uniformly keeps :meth:`dispose`
        symmetric with what was registered.
        """
        fused = CancelSignal()
        for source in signals:
            if source is None:
                continue
            if source.aborted:
                fused.abort(source.reason)
                continue
            fused._detachers.append(
                source.add_listener(lambda s=source: fused.abort(s.reason))
            )
        return fused

    def dispose(self) -> None:
        """Detach from the sources this signal was fused from.

        Not a cancellation: a disposed signal keeps whatever state it has. This
        exists so a short-lived fused signal does not leave a listener behind on
        a long-lived source.
        """
        for detach in self._detachers:
            detach()
        self._detachers.clear()


__all__ = ["CancelSignal", "CancelledError"]
