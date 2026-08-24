"""Session log invariants — Requirement 1.

White-box unit tests for the event-sourced log: contiguous sequence numbers,
immutability, surface membership, and the derive projection.
"""

from __future__ import annotations

import pytest

from pydsh.session import InvalidEventData, Session, SessionError, UnknownEventType


class _Ctx:
    """A minimal stand-in for the plugkit context: records emits."""

    def __init__(self):
        self.emitted = []

    def emit(self, name, *args):
        self.emitted.append((name, *args))


def make_session() -> Session:
    return Session(_Ctx(), id="s1")


def test_starts_empty():
    s = make_session()
    assert s.seq == 0
    assert s.events == ()
    assert s.derive_messages() == []


def test_append_is_contiguous_and_ordered():
    s = make_session()
    ev1 = s.append("turn/start", {"turn": 1})
    ev2 = s.append("user/message", {"content": "hi", "role": "user", "source": {}})
    assert [e.seq for e in s.events] == [1, 2]
    assert ev1.seq == 1 and ev2.seq == 2
    assert ev1.type == "turn/start"


def test_surface_membership_and_derive():
    s = make_session()
    s.append("turn/start", {"turn": 1})
    s.append("user/message", {"content": "hello", "role": "user", "source": {}})
    s.append("assistant/message", {
        "turn": 1, "step": 1,
        "message": {"content": "hi there", "role": "assistant"},
    })
    s.append("tool/call", {"turn": 1, "step": 1, "callId": "c1", "name": "f", "arguments": "{}"})
    # Only the three surface events project.
    assert s.surface_nodes == [2, 3]
    msgs = s.derive_messages()
    assert len(msgs) == 2
    assert msgs[0]["content"] == "hello"
    assert msgs[1]["content"] == "hi there"


def test_tool_result_derives_its_message():
    s = make_session()
    s.append("tool/result", {
        "turn": 1, "step": 1,
        "message": {"role": "user", "content": [{"text": "the answer", "type": "text"}]},
    })
    assert s.derive_messages()[0]["content"][0]["text"] == "the answer"


def test_events_are_immutable():
    s = make_session()
    ev = s.append("user/message", {"content": "x", "role": "user", "source": {}})
    # The event is a frozen dataclass: its own fields cannot be reassigned.
    with pytest.raises(Exception):
        ev.seq = 99
    # The log view is a fresh tuple, so callers cannot mutate `s.events`.
    events = s.events
    with pytest.raises(Exception):
        events[0] = ev


def test_unknown_event_type_rejected():
    s = make_session()
    with pytest.raises(UnknownEventType):
        s.append("no/such", {})


def test_non_lossless_json_rejected():
    s = make_session()
    with pytest.raises(InvalidEventData):
        s.append("user/message", {"content": float("nan"), "role": "user", "source": {}})
    assert s.seq == 0  # nothing was written


def test_cyclic_data_rejected():
    s = make_session()
    cyc = {"content": "boom"}
    cyc["self"] = cyc
    with pytest.raises(InvalidEventData):
        s.append("user/message", cyc)
    assert s.seq == 0


def test_to_from_json_round_trips():
    s = make_session()
    s.append("turn/start", {"turn": 1})
    s.append("user/message", {"content": "hi", "role": "user", "source": {}})
    rebuilt = Session.from_json(_Ctx(), s.to_json())
    assert rebuilt.events == s.events
    assert rebuilt.surface_nodes == s.surface_nodes
    assert rebuilt.derive_messages() == s.derive_messages()
