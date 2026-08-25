"""Deadlines and watchdogs — Requirement 6."""

from __future__ import annotations

import asyncio

import pytest

from pydsh.cancel import CancelSignal
from pydsh.capability import (
    MAX_TIMER_DELAY_MS,
    IdleWatchdog,
    TimeoutReason,
    assert_timer_delay,
    clamp_timeout,
    deadline,
    timeout_of,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# clamp_timeout (R6.5)
# --------------------------------------------------------------------------- #
async def test_a_missing_request_takes_the_default():
    assert clamp_timeout(None, default=1000, maximum=5000) == 1000


async def test_a_request_is_honoured_up_to_the_cap():
    assert clamp_timeout(2000, default=1000, maximum=5000) == 2000


async def test_a_request_over_the_cap_is_capped():
    assert clamp_timeout(9000, default=1000, maximum=5000) == 5000


async def test_a_zero_request_is_rejected():
    """Zero is not a public 'disable the timeout' sentinel — it is arithmetic
    that went wrong, and reading it as 'unbounded' is the worst interpretation.
    """
    with pytest.raises(ValueError, match="positive"):
        clamp_timeout(0, default=1000, maximum=5000)


async def test_a_negative_or_infinite_request_is_rejected():
    for bad in (-1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            clamp_timeout(bad, default=1000, maximum=5000)


async def test_a_non_numeric_request_is_rejected():
    with pytest.raises(TypeError):
        clamp_timeout("soon", default=1000, maximum=5000)


async def test_a_delay_beyond_the_scheduler_limit_is_rejected():
    with pytest.raises(ValueError):
        assert_timer_delay(MAX_TIMER_DELAY_MS + 1)


# --------------------------------------------------------------------------- #
# deadline (R6.1–R6.4)
# --------------------------------------------------------------------------- #
async def test_a_deadline_aborts_when_time_runs_out():
    with deadline(None, 50, "test-timeout") as bound:
        assert bound.signal.aborted is False
        await asyncio.sleep(0.12)
        assert bound.signal.aborted is True


async def test_a_timeout_reason_is_identifiable_as_one():
    """R6.2 (I4) — 'you ran out of time' and 'someone stopped you' differ."""
    with deadline(None, 30, "shell-timeout") as bound:
        await asyncio.sleep(0.1)
        reason = timeout_of(bound.signal.reason)
        assert isinstance(reason, TimeoutReason)
        assert reason.code == "shell-timeout"
        assert reason.timeout_ms == 30


async def test_an_upstream_cancellation_is_not_a_timeout():
    upstream = CancelSignal()
    with deadline(upstream, 5000, "shell-timeout") as bound:
        upstream.abort("the user pressed stop")
        assert bound.signal.aborted is True
        assert timeout_of(bound.signal.reason) is None


async def test_whichever_fires_first_wins():
    upstream = CancelSignal()
    with deadline(upstream, 5000, "slow") as bound:
        upstream.abort("first")
        await asyncio.sleep(0)
        assert bound.signal.reason == "first"


async def test_disposing_cancels_the_timer():
    """R6.3 — a disposed deadline must not fire later."""
    bound = deadline(None, 40, "test")
    bound.dispose()
    await asyncio.sleep(0.1)
    assert bound.signal.aborted is False


async def test_disposing_twice_is_safe():
    bound = deadline(None, 40, "test")
    bound.dispose()
    bound.dispose()


async def test_a_non_positive_timeout_means_no_timer():
    """R6.4 — background work still honours an upstream cancellation."""
    upstream = CancelSignal()
    with deadline(upstream, 0, "test") as bound:
        await asyncio.sleep(0.05)
        assert bound.signal.aborted is False
        upstream.abort("stop")
        assert bound.signal.aborted is True


async def test_a_deadline_with_no_upstream_and_no_timer_never_aborts():
    with deadline(None, 0, "test") as bound:
        await asyncio.sleep(0.05)
        assert bound.signal.aborted is False


# --------------------------------------------------------------------------- #
# IdleWatchdog (R6.6)
# --------------------------------------------------------------------------- #
async def test_a_watchdog_fires_after_the_idle_window():
    with IdleWatchdog(None, 50) as watchdog:
        await asyncio.sleep(0.12)
        assert watchdog.signal.aborted is True


async def test_activity_re_arms_the_watchdog():
    """R6.6 — a stream that is slow but alive must not be killed for it."""
    with IdleWatchdog(None, 80) as watchdog:
        for _ in range(4):
            await asyncio.sleep(0.04)
            watchdog.touch()
        assert watchdog.signal.aborted is False
        await asyncio.sleep(0.15)
        assert watchdog.signal.aborted is True


async def test_a_watchdog_carries_a_timeout_reason():
    with IdleWatchdog(None, 30, "stream-idle") as watchdog:
        await asyncio.sleep(0.1)
        assert timeout_of(watchdog.signal.reason).code == "stream-idle"


async def test_an_upstream_cancellation_reaches_the_watchdog():
    upstream = CancelSignal()
    with IdleWatchdog(upstream, 5000) as watchdog:
        upstream.abort("stop")
        assert watchdog.signal.aborted is True


async def test_disposing_a_watchdog_stops_it():
    watchdog = IdleWatchdog(None, 40)
    watchdog.dispose()
    await asyncio.sleep(0.1)
    assert watchdog.signal.aborted is False


async def test_touching_a_disposed_watchdog_does_nothing():
    watchdog = IdleWatchdog(None, 40)
    watchdog.dispose()
    watchdog.touch()
    await asyncio.sleep(0.1)
    assert watchdog.signal.aborted is False
