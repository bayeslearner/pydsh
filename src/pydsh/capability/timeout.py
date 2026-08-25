"""Deadlines — a bound on how long something may take, as a cancel signal.

Built on :mod:`pydsh.cancel` rather than beside it, so a caller that already
holds a cancel signal gets *one* signal to watch: whichever fires first wins,
and the abort reason says which.

That distinction is the point of :class:`TimeoutReason`. "You ran out of time"
and "someone pressed stop" call for different responses — the first is often
worth retrying and the second never is — and a caller that cannot tell them
apart has to guess.

The watchdog is the other shape: a bound that re-arms on activity. A stream
that is slow but alive should not be killed for being slow, and time the
*consumer* spends thinking should not count against the producer.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any, Optional

from ..cancel import CancelSignal

#: The largest delay a scheduler will honour, in milliseconds. Beyond this a
#: timer either overflows or never fires, which is worse than refusing it.
MAX_TIMER_DELAY_MS = 2_147_483_647


class TimeoutReason(Exception):
    """A cancellation caused by time running out, rather than by a caller."""

    def __init__(self, code: str, timeout_ms: float) -> None:
        super().__init__(f"{code} after {timeout_ms}ms")
        self.code = code
        self.timeout_ms = timeout_ms


def timeout_of(value: Any) -> Optional[TimeoutReason]:
    """The timeout behind an abort reason, or ``None`` if it was not one."""
    return value if isinstance(value, TimeoutReason) else None


def assert_timer_delay(timeout_ms: float, name: str = "timeout_ms") -> None:
    """Reject a delay a timer cannot actually honour."""
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, (int, float)):
        raise TypeError(f"{name} must be a number, got {timeout_ms!r}")
    if not (math.isfinite(timeout_ms) and 0 < timeout_ms <= MAX_TIMER_DELAY_MS):
        raise ValueError(
            f"{name} must be a finite number above 0 and at most "
            f"{MAX_TIMER_DELAY_MS}, got {timeout_ms!r}"
        )


def clamp_timeout(
    requested: Optional[float],
    default: float,
    maximum: float,
    name: str = "timeout_ms",
) -> float:
    """A caller's requested bound, defaulted and capped.

    ``0`` is deliberately *not* a public "disable the timeout" sentinel: a
    caller asking for zero almost always means a mistake in arithmetic, and
    silently turning that into "unbounded" is the worst reading of it.
    """
    if requested is not None:
        if isinstance(requested, bool) or not isinstance(requested, (int, float)):
            raise TypeError(f"{name} must be a number, got {requested!r}")
        if not (math.isfinite(requested) and requested > 0):
            raise ValueError(f"{name} must be a positive finite number, got {requested!r}")
    return min(default if requested is None else requested, maximum)


class Deadline:
    """A signal that aborts when its time runs out, plus the timer's cleanup."""

    def __init__(self, signal: CancelSignal, cancel_timer: Any = None) -> None:
        self.signal = signal
        self._cancel_timer = cancel_timer
        self._fused = signal

    def dispose(self) -> None:
        """Cancel the timer and detach. Safe to call more than once."""
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        self._fused.dispose()

    def __enter__(self) -> "Deadline":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.dispose()


def deadline(
    upstream: Optional[CancelSignal], timeout_ms: float, code: str
) -> Deadline:
    """Fuse a caller's cancellation with a bound on time.

    :param timeout_ms: non-positive means "no timer" — an internal sentinel for
        background work that should still honour an upstream cancellation.
    """
    if timeout_ms <= 0:
        return Deadline(CancelSignal.any([upstream]))

    assert_timer_delay(timeout_ms, "deadline timeout_ms")
    timer = CancelSignal()
    loop = asyncio.get_running_loop()
    handle = loop.call_later(
        timeout_ms / 1000.0, lambda: timer.abort(TimeoutReason(code, timeout_ms))
    )
    return Deadline(CancelSignal.any([upstream, timer]), handle.cancel)


class IdleWatchdog:
    """A bound on *inactivity*, re-armed by every sign of life.

    For a stream: a provider that is slow but still sending should not be
    killed, and the time a consumer spends handling what it received is not the
    provider's fault. So the timer exists only between activity, and
    :meth:`touch` restarts it.
    """

    def __init__(
        self,
        upstream: Optional[CancelSignal],
        idle_ms: float,
        code: str = "idle-timeout",
    ) -> None:
        assert_timer_delay(idle_ms, "idle_ms")
        self._idle_ms = idle_ms
        self._code = code
        self._timer = CancelSignal()
        self.signal = CancelSignal.any([upstream, self._timer])
        self._handle: Optional[asyncio.TimerHandle] = None
        self._disposed = False
        self.touch()

    def touch(self) -> None:
        """Something happened: start the idle window again."""
        if self._disposed:
            return
        if self._handle is not None:
            self._handle.cancel()
        loop = asyncio.get_running_loop()
        self._handle = loop.call_later(
            self._idle_ms / 1000.0,
            lambda: self._timer.abort(TimeoutReason(self._code, self._idle_ms)),
        )

    def dispose(self) -> None:
        """Stop watching. Safe to repeat."""
        self._disposed = True
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        self.signal.dispose()

    def __enter__(self) -> "IdleWatchdog":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.dispose()


__all__ = [
    "TimeoutReason",
    "timeout_of",
    "Deadline",
    "deadline",
    "clamp_timeout",
    "assert_timer_delay",
    "IdleWatchdog",
    "MAX_TIMER_DELAY_MS",
]
