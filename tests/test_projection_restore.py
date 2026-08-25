"""The cold-read ladder — Requirement 3, property 3.

Reading without a live session: stored rows alone, or rows plus a tail of the
log. The gates that matter are the version check and the watermark check, and
the refusal to fold from `init` over a partial history — which would produce a
confident number computed from half the evidence.
"""

from __future__ import annotations

import pytest

from plugkit import Context

from pydsh.session import (
    EMPTY_WATERMARK,
    FIRST_SEQ,
    ProjectionDefinition,
    SessionProjections,
    SessionStore,
)

pytestmark = pytest.mark.asyncio


def counter(key: str = "count", version: int = 1):
    return ProjectionDefinition(
        key=key,
        init=lambda: {"n": 0},
        apply=lambda s, e: {"n": s["n"] + 1} if e.type == "turn/start" else s,
        view=lambda s: s["n"],
        state_version=version,
    )


async def mounted():
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(SessionProjections)
    return root, root.sessions.create()


def starts(session, n: int) -> None:
    for i in range(1, n + 1):
        session.append("turn/start", {"turn": i})


# --------------------------------------------------------------------------- #
# checkpoint (R3.3) — property 3
# --------------------------------------------------------------------------- #
async def test_checkpoint_carries_version_watermark_and_state():
    root, session = await mounted()
    root.session_projections.register(counter())
    starts(session, 2)
    rows = root.session_projections.checkpoint(session)
    assert rows == {"count": {"ver": 1, "seq": 2, "val": {"n": 2}}}


async def test_a_checkpoint_is_detached_from_the_live_cell():
    """Property 3 (I3) — the cells are the registry's authoritative state."""
    root, session = await mounted()
    root.session_projections.register(counter())
    starts(session, 1)

    rows = root.session_projections.checkpoint(session)
    rows["count"]["val"]["n"] = 9999

    assert root.session_projections.snapshot(session)["values"]["count"] == 1


async def test_checkpointing_an_untouched_session_folds_it_first():
    root, session = await mounted()
    starts(session, 3)
    root.session_projections.register(counter())  # after the events
    rows = root.session_projections.checkpoint(session)
    assert rows["count"]["val"] == {"n": 3}


# --------------------------------------------------------------------------- #
# view_checkpoint (R3.4) — the zero-I/O rung
# --------------------------------------------------------------------------- #
async def test_view_checkpoint_reads_rows_without_a_session():
    root, _ = await mounted()
    root.session_projections.register(counter())
    values = root.session_projections.view_checkpoint(
        {"count": {"ver": 1, "seq": 7, "val": {"n": 7}}}
    )
    assert values == {"count": 7}


async def test_a_row_from_another_version_is_absent_not_wrong():
    """R3.4 (I4) — a cold consumer reads absence as 'not available yet'."""
    root, _ = await mounted()
    root.session_projections.register(counter(version=2))
    assert root.session_projections.view_checkpoint(
        {"count": {"ver": 1, "seq": 7, "val": {"n": 7}}}
    ) == {}


async def test_a_missing_row_is_simply_absent():
    root, _ = await mounted()
    root.session_projections.register(counter())
    assert root.session_projections.view_checkpoint({}) == {}


# --------------------------------------------------------------------------- #
# restore_floor (R3.5)
# --------------------------------------------------------------------------- #
async def test_restore_floor_is_none_with_no_units():
    root, _ = await mounted()
    assert root.session_projections.restore_floor({}) is None


async def test_restore_floor_with_no_usable_row_starts_at_the_beginning():
    root, _ = await mounted()
    root.session_projections.register(counter())
    assert root.session_projections.restore_floor({}) == EMPTY_WATERMARK


async def test_restore_floor_follows_the_lowest_watermark():
    root, _ = await mounted()
    root.session_projections.register(counter("a"))
    root.session_projections.register(counter("b"))
    floor = root.session_projections.restore_floor(
        {
            "a": {"ver": 1, "seq": 10, "val": {"n": 10}},
            "b": {"ver": 1, "seq": 4, "val": {"n": 4}},
        }
    )
    assert floor == 4  # the unit that has seen least decides how far back to read


# --------------------------------------------------------------------------- #
# restore (R3.6, R3.7)
# --------------------------------------------------------------------------- #
async def test_restore_seeds_from_a_row_and_folds_the_tail():
    root, session = await mounted()
    root.session_projections.register(counter())
    starts(session, 5)
    events = list(session.events)

    result = root.session_projections.restore(
        {"count": {"ver": 1, "seq": 3, "val": {"n": 3}}},
        events[3:],  # only the tail from seq 4
        base_seq=4,
    )
    assert result["snapshot"]["values"]["count"] == 5
    assert result["snapshot"]["as_of_seq"] == 5
    assert result["checkpoint"]["count"] == {"ver": 1, "seq": 5, "val": {"n": 5}}


async def test_restore_ignores_events_the_row_already_covered():
    """The overlap `restore_floor` deliberately allows must not double-count."""
    root, session = await mounted()
    root.session_projections.register(counter())
    starts(session, 5)

    result = root.session_projections.restore(
        {"count": {"ver": 1, "seq": 3, "val": {"n": 3}}},
        list(session.events),  # the whole log, including what the row covered
        base_seq=FIRST_SEQ,
    )
    assert result["snapshot"]["values"]["count"] == 5


async def test_restore_from_the_start_refolds_when_no_row_is_usable():
    root, session = await mounted()
    root.session_projections.register(counter())
    starts(session, 4)

    result = root.session_projections.restore(
        {}, list(session.events), base_seq=FIRST_SEQ
    )
    assert result["snapshot"]["values"]["count"] == 4


async def test_restore_refuses_a_partial_log_with_an_unusable_row():
    """R3.7 — the sharpest edge on the ladder.

    Folding from `init` over a tail would return a confident number computed
    from half a history, and nothing downstream could tell.
    """
    root, session = await mounted()
    root.session_projections.register(counter())
    starts(session, 5)

    with pytest.raises(RuntimeError, match="re-read the log from the start"):
        root.session_projections.restore({}, list(session.events)[3:], base_seq=4)


async def test_restore_refuses_a_row_from_another_version_over_a_tail():
    root, session = await mounted()
    root.session_projections.register(counter(version=2))
    starts(session, 5)

    with pytest.raises(RuntimeError, match="versioned differently"):
        root.session_projections.restore(
            {"count": {"ver": 1, "seq": 3, "val": {"n": 3}}},
            list(session.events)[3:],
            base_seq=4,
        )


async def test_restore_refuses_a_row_ahead_of_the_evidence():
    """A row cannot be trusted past the events supplied to check it."""
    root, session = await mounted()
    root.session_projections.register(counter())
    starts(session, 3)

    with pytest.raises(RuntimeError, match="ahead of the supplied events"):
        root.session_projections.restore(
            {"count": {"ver": 1, "seq": 99, "val": {"n": 99}}},
            list(session.events)[2:],
            base_seq=3,
        )


async def test_restore_with_an_empty_tail_reports_the_row_as_it_stands():
    root, _ = await mounted()
    root.session_projections.register(counter())
    result = root.session_projections.restore(
        {"count": {"ver": 1, "seq": 6, "val": {"n": 6}}}, [], base_seq=7
    )
    assert result["snapshot"] == {"as_of_seq": 6, "values": {"count": 6}}


async def test_restore_does_not_alias_the_row_it_was_given():
    """Seeding from a row must copy it: the caller still owns that dict."""
    root, session = await mounted()
    root.session_projections.register(counter())
    starts(session, 4)
    rows = {"count": {"ver": 1, "seq": 2, "val": {"n": 2}}}

    root.session_projections.restore(rows, list(session.events), base_seq=FIRST_SEQ)
    assert rows["count"]["val"] == {"n": 2}  # untouched by the fold


async def test_the_ladder_round_trips_through_a_checkpoint():
    """The whole point: checkpoint here, restore there, same answer."""
    root, session = await mounted()
    root.session_projections.register(counter())
    starts(session, 3)
    rows = root.session_projections.checkpoint(session)

    starts(session, 2)  # two more events land after the checkpoint
    tail = [e for e in session.events if e.seq > rows["count"]["seq"]]

    result = root.session_projections.restore(
        rows, tail, base_seq=rows["count"]["seq"] + 1
    )
    assert result["snapshot"]["values"]["count"] == 5
    assert root.session_projections.snapshot(session)["values"]["count"] == 5
