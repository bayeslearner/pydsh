"""The cancellation signal — Requirement 1.

Pure logic, no kernel, no event loop. These are the semantics the agent loop
and every adapter rely on, so they are worth pinning precisely.
"""

from __future__ import annotations

import pytest

from pydsh.cancel import CancelledError, CancelSignal


def test_a_fresh_signal_is_live():
    signal = CancelSignal()
    assert signal.aborted is False
    assert signal.reason is None
    signal.throw_if_aborted()  # does not raise


def test_abort_records_and_notifies():
    """R1.1"""
    signal = CancelSignal()
    seen: list[int] = []
    signal.add_listener(lambda: seen.append(1))
    signal.abort("stopped")
    assert signal.aborted is True
    assert signal.reason == "stopped"
    assert seen == [1]


def test_abort_is_idempotent_and_the_first_reason_stands():
    """R1.2 — a second cancellation cannot rewrite why the first happened."""
    signal = CancelSignal()
    calls: list[int] = []
    signal.add_listener(lambda: calls.append(1))
    signal.abort("first")
    signal.abort("second")
    assert signal.reason == "first"
    assert calls == [1]


def test_a_raising_listener_does_not_break_the_abort():
    """R1.3 — cancellation already happened; an observer cannot undo it."""
    signal = CancelSignal()
    seen: list[str] = []

    def bad() -> None:
        raise RuntimeError("observer bug")

    signal.add_listener(bad)
    signal.add_listener(lambda: seen.append("good"))
    signal.abort("stop")
    assert signal.aborted is True
    assert seen == ["good"]


def test_throw_if_aborted_carries_the_reason():
    """R1.4"""
    signal = CancelSignal()
    signal.abort({"kind": "user"})
    with pytest.raises(CancelledError) as caught:
        signal.throw_if_aborted()
    assert caught.value.reason == {"kind": "user"}


def test_detaching_a_listener_stops_it_firing():
    signal = CancelSignal()
    seen: list[int] = []
    detach = signal.add_listener(lambda: seen.append(1))
    assert detach() is True
    assert detach() is False  # already gone
    signal.abort()
    assert seen == []


# --------------------------------------------------------------------------- #
# Fusion
# --------------------------------------------------------------------------- #
def test_any_propagates_from_either_source():
    """R1.5"""
    a, b = CancelSignal(), CancelSignal()
    fused = CancelSignal.any([a, b])
    assert fused.aborted is False
    b.abort("b stopped")
    assert fused.aborted is True
    assert fused.reason == "b stopped"


def test_any_skips_none_entries():
    """A caller passes an optional signal without branching around it."""
    a = CancelSignal()
    fused = CancelSignal.any([None, a, None])
    a.abort("x")
    assert fused.reason == "x"


def test_any_of_an_already_aborted_source_is_aborted_at_once():
    """R1.6 — creating an agent under a dead parent must not look alive."""
    a = CancelSignal()
    a.abort("already gone")
    fused = CancelSignal.any([a])
    assert fused.aborted is True
    assert fused.reason == "already gone"


def test_the_first_source_to_abort_wins():
    a, b = CancelSignal(), CancelSignal()
    fused = CancelSignal.any([a, b])
    a.abort("a")
    b.abort("b")
    assert fused.reason == "a"


def test_dispose_detaches_from_every_source():
    """R1.7 — the leak this fixes: one listener per agent on a long-lived source."""
    teardown = CancelSignal()
    for _ in range(100):
        fused = CancelSignal.any([teardown])
        fused.dispose()
    # The loop fuses this signal once per agent it creates; without dispose the
    # listeners would pile up for the life of the process.
    assert teardown._listeners == []


def test_a_disposed_signal_stops_hearing_its_sources():
    source = CancelSignal()
    fused = CancelSignal.any([source])
    fused.dispose()
    source.abort("too late")
    assert fused.aborted is False


def test_dispose_is_not_a_cancellation():
    """Disposing detaches; it does not decide the scope's outcome."""
    source = CancelSignal()
    fused = CancelSignal.any([source])
    fused.abort("done")
    fused.dispose()
    assert fused.aborted is True
    assert fused.reason == "done"
