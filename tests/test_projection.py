"""The projection registry — Requirements 1 and 2, properties 1 and 2.

The two claims worth the most: a unit registered mid-stream reaches the same
state as one that was there all along, and a unit that ignores an event costs
nothing on it.
"""

from __future__ import annotations

import pytest

from plugkit import Context

from pydsh.session import ProjectionDefinition, SessionProjections, SessionStore
from pydsh.session.projection import ProjectionFaultError

pytestmark = pytest.mark.asyncio


def counter(key: str = "count", *, of: str = "turn/start", version: int = 1):
    """A unit that counts one event type and ignores everything else."""
    return ProjectionDefinition(
        key=key,
        init=lambda: {"n": 0},
        apply=lambda state, event: (
            {"n": state["n"] + 1} if event.type == of else state
        ),
        view=lambda state: state["n"],
        state_version=version,
    )


async def mounted() -> tuple[Context, object]:
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(SessionProjections)
    return root, root.sessions.create()


def turns(session, n: int) -> None:
    for i in range(1, n + 1):
        session.append("turn/start", {"turn": i})
        session.append("turn/end", {"turn": i, "reason": {"kind": "completed"}})


# --------------------------------------------------------------------------- #
# Registration (R1)
# --------------------------------------------------------------------------- #
async def test_a_unit_is_driven_once_registered():
    root, session = await mounted()
    root.session_projections.register(counter())
    turns(session, 3)
    assert root.session_projections.snapshot(session)["values"]["count"] == 3


async def test_no_units_snapshots_empty_rather_than_failing():
    """R3.2 — a consumer reading before any domain mounts gets an answer."""
    root, session = await mounted()
    assert root.session_projections.snapshot(session) == {
        "as_of_seq": 0,
        "values": {},
    }


async def test_a_negative_state_version_is_rejected():
    root, _ = await mounted()
    with pytest.raises(ValueError, match="non-negative integer"):
        root.session_projections.register(counter(version=-1))


async def test_a_non_integer_state_version_is_rejected():
    root, _ = await mounted()
    with pytest.raises(ValueError, match="non-negative integer"):
        root.session_projections.register(counter(version="1"))


async def test_two_registrants_of_one_key_share_a_cell():
    """R1.3 (I5) — the same plugin under N presets is normal, not a conflict."""
    root, session = await mounted()
    first = root.session_projections.register(counter())
    second = root.session_projections.register(counter())
    turns(session, 2)

    assert root.session_projections.snapshot(session)["values"]["count"] == 2
    first()
    assert "count" in root.session_projections.keys()  # the second still holds it
    second()
    assert root.session_projections.keys() == []


async def test_a_conflicting_state_version_is_refused():
    """R1.4 — a versioned contract says the cached shape is not the same."""
    root, _ = await mounted()
    root.session_projections.register(counter(version=1))
    with pytest.raises(ValueError, match="refusing to share"):
        root.session_projections.register(counter(version=2))


async def test_disposing_twice_does_not_drop_another_registrant():
    root, _ = await mounted()
    dispose = root.session_projections.register(counter())
    root.session_projections.register(counter())
    dispose()
    dispose()  # a stale handle must not consume the other registrant's ref
    assert root.session_projections.keys() == ["count"]


# --------------------------------------------------------------------------- #
# Driving (R2), properties 1 and 2
# --------------------------------------------------------------------------- #
async def test_a_unit_registered_mid_stream_catches_up():
    """Property 1 (I1) — rebuild equals drive."""
    root, session = await mounted()
    turns(session, 2)  # before anything is registered
    root.session_projections.register(counter())
    turns(session, 1)  # and one after
    assert root.session_projections.snapshot(session)["values"]["count"] == 3


async def test_a_mid_stream_unit_does_not_double_apply_the_current_event():
    """The prefix is strictly before the event that triggered the build."""
    root, session = await mounted()
    root.session_projections.register(counter(of="turn/end"))
    turns(session, 1)
    assert root.session_projections.snapshot(session)["values"]["count"] == 1


async def test_an_ignored_event_publishes_nothing_and_computes_no_view():
    """Property 2 (I2) — identity, not equality, is the change gate."""
    views = 0

    def view(state):
        nonlocal views
        views += 1
        return state["n"]

    root, session = await mounted()
    root.session_projections.register(
        ProjectionDefinition(
            key="count",
            init=lambda: {"n": 0},
            apply=lambda s, e: {"n": s["n"] + 1} if e.type == "turn/start" else s,
            view=view,
        )
    )
    changes = []
    root.session_projections.on_changed(
        lambda session, key, value, seq: changes.append((key, value, seq))
    )

    session.append("turn/start", {"turn": 1})  # counted
    session.append("step/start", {"turn": 1, "step": 1})  # ignored
    session.append("step/end", {"turn": 1, "step": 1})  # ignored

    assert [c[1] for c in changes] == [1]
    assert views == 1  # only the event that changed anything cost a view


async def test_a_change_carries_the_session_key_value_and_seq():
    root, session = await mounted()
    root.session_projections.register(counter())
    seen = []
    root.session_projections.on_changed(
        lambda s, key, value, seq: seen.append((s, key, value, seq))
    )
    event = session.append("turn/start", {"turn": 1})

    assert seen == [(session, "count", 1, event.seq)]


async def test_unsubscribing_stops_the_stream():
    root, session = await mounted()
    root.session_projections.register(counter())
    seen = []
    off = root.session_projections.on_changed(lambda *a: seen.append(a))
    session.append("turn/start", {"turn": 1})
    off()
    session.append("turn/start", {"turn": 2})
    assert len(seen) == 1


async def test_the_same_listener_twice_unsubscribes_one_at_a_time():
    """Identity decides which registration goes, not equality."""
    root, session = await mounted()
    root.session_projections.register(counter())
    seen = []

    def listener(*args):
        seen.append(args)

    off = root.session_projections.on_changed(listener)
    root.session_projections.on_changed(listener)
    off()
    session.append("turn/start", {"turn": 1})
    assert len(seen) == 1


async def test_a_raising_listener_does_not_stop_the_others():
    """R2.6 — the change already happened; one bad observer must not undo it."""
    root, session = await mounted()
    root.session_projections.register(counter())
    seen = []

    def bad(*args):
        raise RuntimeError("observer bug")

    root.session_projections.on_changed(bad)
    root.session_projections.on_changed(lambda *a: seen.append(a))
    session.append("turn/start", {"turn": 1})

    assert len(seen) == 1
    assert root.session_projections.snapshot(session)["values"]["count"] == 1


async def test_a_raising_apply_faults_the_cell_without_breaking_the_append():
    """Both halves matter.

    The append must stand: `session/event` is a contained post-commit
    broadcast, and spec 01's rule is that an observer never rewrites history.
    But the cell has now missed a transition, so it must not go on serving a
    plausible number — reads fault instead.
    """
    root, session = await mounted()
    root.session_projections.register(counter("good"))
    root.session_projections.register(
        ProjectionDefinition(
            key="boom",
            init=lambda: 0,
            apply=lambda s, e: (_ for _ in ()).throw(RuntimeError("bad maths")),
            view=lambda s: s,
        )
    )

    event = session.append("turn/start", {"turn": 1})
    assert event.seq == 1  # the append committed

    with pytest.raises(ProjectionFaultError, match="boom"):
        root.session_projections.snapshot(session)


async def test_a_faulted_unit_does_not_stop_the_others_being_driven():
    root, session = await mounted()
    good = counter("good")
    root.session_projections.register(
        ProjectionDefinition(
            key="boom",
            init=lambda: 0,
            apply=lambda s, e: (_ for _ in ()).throw(RuntimeError("bad maths")),
            view=lambda s: s,
        )
    )
    root.session_projections.register(good)
    turns(session, 2)

    # The healthy unit kept counting; only the broken key is unreadable.
    assert root.session_projections.view_checkpoint(
        {"good": {"ver": 1, "seq": 4, "val": {"n": 2}}}
    )["good"] == 2


async def test_a_fault_is_per_session():
    """One session's bad event does not blind the unit everywhere."""
    root, session = await mounted()
    other = root.sessions.create()
    root.session_projections.register(
        ProjectionDefinition(
            key="picky",
            init=lambda: 0,
            apply=lambda s, e: (
                (_ for _ in ()).throw(RuntimeError("nope"))
                if e.data.get("turn") == 99
                else s + 1
            ),
            view=lambda s: s,
        )
    )
    session.append("turn/start", {"turn": 99})
    other.append("turn/start", {"turn": 1})

    with pytest.raises(ProjectionFaultError):
        root.session_projections.snapshot(session)
    assert root.session_projections.snapshot(other)["values"]["picky"] == 1


async def test_two_units_are_driven_independently():
    root, session = await mounted()
    root.session_projections.register(counter("starts", of="turn/start"))
    root.session_projections.register(counter("ends", of="turn/end"))
    turns(session, 2)
    values = root.session_projections.snapshot(session)["values"]
    assert values == {"starts": 2, "ends": 2}


async def test_cells_are_per_session():
    root, session = await mounted()
    other = root.sessions.create()
    root.session_projections.register(counter())
    turns(session, 2)
    turns(other, 1)
    assert root.session_projections.snapshot(session)["values"]["count"] == 2
    assert root.session_projections.snapshot(other)["values"]["count"] == 1


async def test_a_validator_runs_before_a_value_leaves():
    root, session = await mounted()
    root.session_projections.register(
        ProjectionDefinition(
            key="count",
            init=lambda: 0,
            apply=lambda s, e: s + 1 if e.type == "turn/start" else s,
            view=lambda s: s,
            validate=lambda value: {"checked": value},
        )
    )
    session.append("turn/start", {"turn": 1})
    assert root.session_projections.snapshot(session)["values"]["count"] == {
        "checked": 1
    }


async def test_as_of_seq_tracks_the_last_committed_event():
    """The reference's `seq - 1` assumes its seq is the *next* number; pydsh's
    is the last committed one, so transcribing it reports every snapshot one
    event stale."""
    root, session = await mounted()
    root.session_projections.register(counter())
    assert root.session_projections.snapshot(session)["as_of_seq"] == 0
    event = session.append("turn/start", {"turn": 1})
    assert root.session_projections.snapshot(session)["as_of_seq"] == event.seq
