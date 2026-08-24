"""The inbox — Requirement 2 and property 3.

The claim under test is that the queues are a *projection*: everything about
them is in the log, so a restart rebuilds them exactly. The replay tests are
that proof; the rest pin the splice shape the reference defines.
"""

from __future__ import annotations

import pytest

from plugkit import Context

from pydsh.agent.inbox import NEXT_STEP, NEXT_TURN, SPLICE_EVENT, Inbox
from pydsh.message import MessageSource, TextBlock, create_user_message
from pydsh.session import SessionStore

pytestmark = pytest.mark.asyncio


async def session_and_inbox(notifications=None) -> tuple:
    root = Context()
    await root.plugin(SessionStore)
    session = root.sessions.create()
    return session, Inbox(session, notifications)


def message(text: str):
    return create_user_message([TextBlock(text)], MessageSource("user"))


def splices(session) -> list[dict]:
    return [e.data for e in session.events if e.type == SPLICE_EVENT]


def texts(messages) -> list[str]:
    return [m.content[0].text for m in messages]


# --------------------------------------------------------------------------- #
# The queues
# --------------------------------------------------------------------------- #
async def test_starts_empty():
    _session, inbox = await session_and_inbox()
    assert inbox.next_turn == []
    assert inbox.next_step == []
    assert inbox.has_pending is False


async def test_append_and_prepend_order():
    """R2.1"""
    _session, inbox = await session_and_inbox()
    inbox.append(NEXT_TURN, message("second"))
    inbox.prepend(NEXT_TURN, message("first"))
    assert texts(inbox.next_turn) == ["first", "second"]
    assert inbox.has_pending is True


async def test_the_views_are_copies():
    """A caller must not be able to mutate the projection behind its back."""
    _session, inbox = await session_and_inbox()
    inbox.append(NEXT_TURN, message("one"))
    inbox.next_turn.clear()
    assert len(inbox.next_turn) == 1


async def test_an_unknown_target_is_rejected():
    _session, inbox = await session_and_inbox()
    with pytest.raises(ValueError, match="unknown inbox target"):
        inbox.append("next-decade", message("one"))


# --------------------------------------------------------------------------- #
# The splice events (R2.2, R2.3, R2.6)
# --------------------------------------------------------------------------- #
async def test_every_change_is_recorded_before_it_happens():
    session, inbox = await session_and_inbox()
    inbox.append(NEXT_TURN, message("one"))
    recorded = splices(session)
    assert len(recorded) == 1
    assert recorded[0]["target"] == NEXT_TURN
    assert recorded[0]["start"] == 0
    assert "removedCount" not in recorded[0]


async def test_a_cancelling_removal_says_so():
    """R2.3 — removed with nothing put back is a cancellation, and reads as one."""
    session, inbox = await session_and_inbox()
    msg = message("one")
    inbox.append(NEXT_TURN, msg)
    assert inbox.remove(msg.id) is True

    removal = splices(session)[-1]
    assert removal["removedCount"] == 1
    assert removal["outcome"] == "canceled"
    assert inbox.has_pending is False


async def test_removing_something_absent_changes_nothing():
    session, inbox = await session_and_inbox()
    inbox.append(NEXT_TURN, message("one"))
    before = len(session.events)
    assert inbox.remove("no-such-id") is False
    assert len(session.events) == before


async def test_a_no_op_change_is_not_recorded():
    """Clearing an empty inbox did not happen, so the log must not say it did."""
    session, inbox = await session_and_inbox()
    inbox.clear()
    assert splices(session) == []


async def test_the_payload_is_lossless_json():
    """R2.6 — messages are encoded, so the session log accepts them."""
    session, inbox = await session_and_inbox()
    inbox.append(NEXT_TURN, message("hello"))
    inserted = splices(session)[0]["inserted"][0]
    assert "__msg__" in inserted  # the tagged encoding, not a live object


# --------------------------------------------------------------------------- #
# Claiming (R2.4, R2.5)
# --------------------------------------------------------------------------- #
async def test_claim_takes_all_of_next_step_and_one_next_turn():
    session, inbox = await session_and_inbox()
    inbox.append(NEXT_STEP, message("step-a"))
    inbox.append(NEXT_STEP, message("step-b"))
    inbox.append(NEXT_TURN, message("turn-1"))
    inbox.append(NEXT_TURN, message("turn-2"))

    claimed = inbox.claim(NEXT_TURN, turn=1)
    assert texts(claimed) == ["step-a", "step-b", "turn-1"]
    # The second prompt waits: each one opens a turn of its own.
    assert texts(inbox.next_turn) == ["turn-2"]


async def test_claiming_for_a_step_leaves_the_turn_queue_alone():
    session, inbox = await session_and_inbox()
    inbox.append(NEXT_STEP, message("step"))
    inbox.append(NEXT_TURN, message("turn"))
    claimed = inbox.claim(NEXT_STEP, turn=1)
    assert texts(claimed) == ["step"]
    assert texts(inbox.next_turn) == ["turn"]


async def test_notifications_fire_for_each_lifecycle_moment():
    """R2.5"""
    seen: list[tuple[str, str]] = []
    _session, inbox = await session_and_inbox(
        {
            "inserted": lambda m: seen.append(("inserted", m.content[0].text)),
            "claimed": lambda m, turn: seen.append(("claimed", m.content[0].text)),
            "discarded": lambda m: seen.append(("discarded", m.content[0].text)),
        }
    )
    kept = message("kept")
    dropped = message("dropped")
    inbox.append(NEXT_TURN, kept)
    inbox.append(NEXT_TURN, dropped)
    inbox.remove(dropped.id)
    inbox.claim(NEXT_TURN, turn=1)

    assert seen == [
        ("inserted", "kept"),
        ("inserted", "dropped"),
        ("discarded", "dropped"),
        ("claimed", "kept"),
    ]


# --------------------------------------------------------------------------- #
# Property 3 — the projection is faithful
# --------------------------------------------------------------------------- #
async def test_replay_rebuilds_the_queues():
    """R2.7 — the restart case: input delivered but not processed is not lost."""
    session, inbox = await session_and_inbox()
    inbox.append(NEXT_TURN, message("a"))
    inbox.append(NEXT_TURN, message("b"))
    inbox.append(NEXT_STEP, message("c"))

    rebuilt = Inbox.replay(session)
    assert texts(rebuilt.next_turn) == texts(inbox.next_turn)
    assert texts(rebuilt.next_step) == texts(inbox.next_step)


async def test_replay_follows_a_long_mixed_history():
    """Every operation, in an order chosen to move the indices around."""
    session, inbox = await session_and_inbox()
    first = message("first")
    inbox.append(NEXT_TURN, first)
    inbox.append(NEXT_TURN, message("second"))
    inbox.prepend(NEXT_TURN, message("zeroth"))
    inbox.append(NEXT_STEP, message("step-one"))
    inbox.claim(NEXT_TURN, turn=1)
    inbox.append(NEXT_TURN, message("third"))
    inbox.remove(first.id)
    inbox.append(NEXT_STEP, message("step-two"))

    rebuilt = Inbox.replay(session)
    assert texts(rebuilt.next_turn) == texts(inbox.next_turn)
    assert texts(rebuilt.next_step) == texts(inbox.next_step)


async def test_replay_of_a_cleared_inbox_is_empty():
    session, inbox = await session_and_inbox()
    inbox.append(NEXT_TURN, message("a"))
    inbox.append(NEXT_STEP, message("b"))
    inbox.clear()
    rebuilt = Inbox.replay(session)
    assert rebuilt.has_pending is False


async def test_replay_does_not_rerecord_history():
    """A read of the log must not append to it."""
    session, inbox = await session_and_inbox()
    inbox.append(NEXT_TURN, message("a"))
    before = len(session.events)
    Inbox.replay(session)
    assert len(session.events) == before
