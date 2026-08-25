"""Surface replacement — Requirements 1 and 2, properties 1 and 2.

The machinery spec 01 defined and deferred. The two claims that matter: the log
never loses anything, and a reload reproduces the *compacted* surface rather
than the one before it.
"""

from __future__ import annotations

import pytest

from plugkit import Context

from pydsh.message import MessageSource, TextBlock, create_user_message, encode_payload
from pydsh.session import (
    Session,
    SessionError,
    SessionStore,
    SqliteSessionPersistence,
)

pytestmark = pytest.mark.asyncio


def user(text: str) -> dict:
    return encode_payload(
        create_user_message([TextBlock(text)], MessageSource("user"))
    )


async def session_with(count: int) -> tuple[Context, Session]:
    root = Context()
    await root.plugin(SessionStore)
    session = root.sessions.create("chat-1")
    for i in range(count):
        session.append("user/message", user(f"message {i}"))
    return root, session


def texts(session: Session) -> list[str]:
    from pydsh.message import decode_payload

    return [decode_payload(m).content[0].text for m in session.derive_messages()]


# --------------------------------------------------------------------------- #
# Replacing (R1)
# --------------------------------------------------------------------------- #
async def test_a_replacement_swaps_a_range_for_one_node():
    root, session = await session_with(5)
    assert session.surface_nodes == [1, 2, 3, 4, 5]

    event = session.append(
        "user/message",
        user("a summary"),
        surface_op={"op": "replace", "start": 2, "end": 4},
    )

    assert session.surface_nodes == [1, event.seq, 5]
    assert texts(session) == ["message 0", "a summary", "message 4"]


async def test_nothing_is_deleted():
    """Property 1 (I1) — the whole design rests on this."""
    root, session = await session_with(5)
    before = [(e.seq, e.type) for e in session.events]

    session.append(
        "user/message", user("a summary"),
        surface_op={"op": "replace", "start": 2, "end": 4},
    )

    after = [(e.seq, e.type) for e in session.events]
    assert after[: len(before)] == before
    # And every shadowed event is still readable at its original sequence.
    by_seq = {e.seq: e for e in session.events}
    assert all(seq in by_seq for seq in (2, 3, 4))


async def test_the_replace_generation_ticks():
    """R1.4 — a cheap, exact staleness signal for anything caching the surface."""
    root, session = await session_with(4)
    assert session.replace_generation == 0

    session.append("user/message", user("s"),
                   surface_op={"op": "replace", "start": 1, "end": 2})
    assert session.replace_generation == 1

    session.append("user/message", user("t"),
                   surface_op={"op": "replace", "start": 3, "end": 4})
    assert session.replace_generation == 2


async def test_provenance_records_what_was_shadowed():
    root, session = await session_with(4)
    event = session.append(
        "user/message", user("s"),
        surface_op={"op": "replace", "start": 2, "end": 3},
    )
    assert session.replacements == [{"new_seq": event.seq, "shadowed_seqs": [2, 3]}]


async def test_source_event_seqs_are_kept():
    root, session = await session_with(3)
    event = session.append(
        "user/message", user("s"),
        surface_op={"op": "replace", "start": 1, "end": 2},
        source_event_seqs=(1, 2),
    )
    assert event.source_event_seqs == (1, 2)


async def test_a_node_not_on_the_surface_is_refused():
    root, session = await session_with(3)
    with pytest.raises(SessionError, match="not on the surface"):
        session.append("user/message", user("s"),
                       surface_op={"op": "replace", "start": 90, "end": 99})


async def test_an_inverted_run_is_refused():
    """The nodes must name a run in surface order, not any two nodes."""
    root, session = await session_with(4)
    with pytest.raises(SessionError, match="precedes"):
        session.append("user/message", user("s"),
                       surface_op={"op": "replace", "start": 3, "end": 1})


async def test_a_run_is_taken_positionally_not_by_sequence_number():
    """The defect the reference carries, and which only bites the *second* time.

    A replacement puts a high sequence where a low range used to be, so the
    surface stops being ordered by sequence. Selecting with `start <= seq <= end`
    then finds the wrong nodes or none, and compaction works exactly once.
    """
    root, session = await session_with(6)
    first = session.append("user/message", user("summary one"),
                           surface_op={"op": "replace", "start": 1, "end": 3})
    assert session.surface_nodes == [first.seq, 4, 5, 6]
    assert first.seq > 6  # the surface is no longer sorted

    second = session.append("user/message", user("summary two"),
                            surface_op={"op": "replace", "start": first.seq, "end": 6})
    assert session.surface_nodes == [second.seq]


async def test_a_non_surface_event_cannot_replace():
    """R1.7 — a log-only event has no place in the projection to take."""
    root, session = await session_with(3)
    with pytest.raises(SessionError, match="cannot replace surface nodes"):
        session.append("turn/start", {"turn": 1},
                       surface_op={"op": "replace", "start": 1, "end": 2})


async def test_replacing_the_whole_surface_is_allowed():
    root, session = await session_with(4)
    event = session.append("user/message", user("everything"),
                           surface_op={"op": "replace", "start": 1, "end": 4})
    assert session.surface_nodes == [event.seq]
    assert texts(session) == ["everything"]


async def test_a_second_replacement_can_shadow_the_first():
    """Compactions chain; so does the provenance."""
    root, session = await session_with(6)
    first = session.append("user/message", user("summary one"),
                           surface_op={"op": "replace", "start": 1, "end": 3})
    second = session.append("user/message", user("summary two"),
                            surface_op={"op": "replace", "start": first.seq, "end": 6})

    assert session.surface_nodes == [second.seq]
    assert texts(session) == ["summary two"]
    assert session.replacements[1]["shadowed_seqs"] == [first.seq, 4, 5, 6]


async def test_the_surface_is_no_longer_monotonic_after_a_replacement():
    """Worth pinning: a later event now sits where an earlier range was."""
    root, session = await session_with(5)
    event = session.append("user/message", user("s"),
                           surface_op={"op": "replace", "start": 1, "end": 2})
    assert session.surface_nodes == [event.seq, 3, 4, 5]
    assert session.surface_nodes != sorted(session.surface_nodes)


# --------------------------------------------------------------------------- #
# Surviving a reload (R2) — property 2
# --------------------------------------------------------------------------- #
async def test_a_reload_reproduces_the_compacted_surface():
    """Property 2 (I3) — the trap spec 01's `from_json` would have fallen into.

    Rebuilding by collecting every event whose *type* is a surface type
    resurrects exactly what was shadowed, and nothing reports it: the session
    is simply uncompacted again.
    """
    root, session = await session_with(5)
    session.append("user/message", user("a summary"),
                   surface_op={"op": "replace", "start": 2, "end": 4})

    rebuilt = Session.from_json(root, session.to_json())

    assert rebuilt.surface_nodes == session.surface_nodes
    assert texts(rebuilt) == texts(session)
    assert texts(rebuilt) == ["message 0", "a summary", "message 4"]


async def test_a_reload_restores_the_replace_generation():
    """R2.4 — or a cache built before the reload looks current."""
    root, session = await session_with(5)
    session.append("user/message", user("s"),
                   surface_op={"op": "replace", "start": 1, "end": 2})

    rebuilt = Session.from_json(root, session.to_json())
    assert rebuilt.replace_generation == session.replace_generation


async def test_chained_replacements_survive_a_reload():
    root, session = await session_with(6)
    first = session.append("user/message", user("one"),
                           surface_op={"op": "replace", "start": 1, "end": 3})
    session.append("user/message", user("two"),
                   surface_op={"op": "replace", "start": first.seq, "end": 6})

    rebuilt = Session.from_json(root, session.to_json())
    assert texts(rebuilt) == ["two"]


async def test_a_compacted_session_round_trips_through_sqlite(tmp_path):
    """R2.5 — through the real backend, not just the dataclass."""
    root = Context()
    await root.plugin(SessionStore)
    backend = SqliteSessionPersistence(str(tmp_path / "log.db"))
    root.sessions.attach_persistence(backend)

    session = root.sessions.create("chat-1")
    for i in range(5):
        session.append("user/message", user(f"message {i}"))
    session.append("user/message", user("a summary"),
                   surface_op={"op": "replace", "start": 2, "end": 4})
    await root.sessions.flush(session)

    reader = Context()
    await reader.plugin(SessionStore)
    reader.sessions.attach_persistence(SqliteSessionPersistence(str(tmp_path / "log.db")))
    reloaded = await reader.sessions.resume("chat-1")

    assert reloaded.surface_nodes == [1, 6, 5]
    assert texts(reloaded) == ["message 0", "a summary", "message 4"]
    # And nothing was lost on the way to disk.
    assert len(reloaded.events) == 6


async def test_derive_messages_follows_the_surface_not_the_event_types():
    """The distinction that only becomes observable once a replacement exists."""
    root, session = await session_with(3)
    session.append("user/message", user("s"),
                   surface_op={"op": "replace", "start": 1, "end": 3})

    surface_typed = [e for e in session.events if e.type == "user/message"]
    assert len(surface_typed) == 4  # every original, plus the summary
    assert len(session.derive_messages()) == 1  # but the surface holds one
