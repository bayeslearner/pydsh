"""The shared message vocabulary and its lossless-JSON encoding."""

from __future__ import annotations

import json

import pytest

from pydsh.message import (
    Message,
    MessageSource,
    PayloadDecodeError,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    as_text,
    create_assistant_message,
    create_user_message,
    decode_payload,
    encode_payload,
)


# --------------------------------------------------------------------------- #
# Blocks and messages
# --------------------------------------------------------------------------- #
def test_blocks_are_frozen():
    """Content blocks are values — a message's history cannot be edited."""
    block = TextBlock("hello")
    with pytest.raises(Exception):
        block.text = "goodbye"  # type: ignore[misc]


def test_as_text_joins_only_text_blocks():
    content = (
        TextBlock("the answer "),
        ReasoningBlock("let me think"),
        TextBlock("is 42"),
        ToolCallBlock(id="c1", name="calc", arguments='{"x":1}'),
    )
    assert as_text(content) == "the answer is 42"


def test_as_text_of_empty_content():
    assert as_text(()) == ""


def test_create_user_message_defaults_to_user_source():
    m = create_user_message([TextBlock("hi")])
    assert m.role == "user"
    assert m.source.kind == "user"
    assert m.content == (TextBlock("hi"),)
    assert m.id


def test_create_assistant_message_carries_attribution():
    m = create_assistant_message([TextBlock("hi")], provider="acme", model="a-1")
    assert m.role == "assistant"
    assert m.source == MessageSource("model", provider="acme", model="a-1")


def test_message_ids_are_distinct():
    a = create_user_message([TextBlock("x")])
    b = create_user_message([TextBlock("x")])
    assert a.id != b.id


# --------------------------------------------------------------------------- #
# Round-trip (Property 2 / invariant I4)
# --------------------------------------------------------------------------- #
def _roundtrip(value):
    """Encode, force through real JSON, decode — the actual persistence path."""
    encoded = encode_payload(value)
    revived = json.loads(json.dumps(encoded))
    return decode_payload(revived)


@pytest.mark.parametrize(
    "value",
    [
        None,
        0,
        "",
        "text",
        True,
        3.5,
        [1, 2, 3],
        {"a": 1, "b": {"c": "d"}},
        TextBlock("hello"),
        ReasoningBlock("thinking"),
        ToolCallBlock(id="c1", name="bash", arguments='{"cmd":"ls"}'),
    ],
)
def test_roundtrip_is_identity(value):
    assert _roundtrip(value) == value


def test_roundtrip_message_with_every_block_kind():
    message = Message(
        id="m1",
        role="assistant",
        content=(
            TextBlock("here"),
            ReasoningBlock("because"),
            ToolCallBlock(id="c1", name="fs", arguments='{"p":"/tmp"}'),
            ToolResultBlock(
                tool_call_id="c1",
                content=(TextBlock("ok"), ReasoningBlock("nested")),
                is_error=False,
            ),
        ),
        source=MessageSource("model", provider="acme", model="a-1"),
    )
    assert _roundtrip(message) == message


def test_roundtrip_preserves_goal_attribution():
    """The reference drops goal_id/revision/round; we keep them."""
    source = MessageSource("goal", goal_id="g1", revision=3, round=7)
    message = Message(id="m", role="user", content=(TextBlock("go"),), source=source)
    assert _roundtrip(message).source == source


def test_roundtrip_messages_nested_in_containers():
    message = create_user_message([TextBlock("hi")])
    value = {"turn": 1, "messages": [message], "meta": {"inner": message}}
    revived = _roundtrip(value)
    assert revived["messages"][0] == message
    assert revived["meta"]["inner"] == message
    assert revived["turn"] == 1


def test_tuples_normalize_to_lists():
    """JSON has one sequence type — documented, not accidental."""
    assert _roundtrip((1, 2)) == [1, 2]


def test_decode_rejects_unknown_block_tag():
    with pytest.raises(PayloadDecodeError, match="unknown content block tag"):
        decode_payload({"__block__": "hologram", "text": "x"})


def test_encoded_payload_is_lossless_json():
    """Invariant I4: what we hand the session log must satisfy its validator."""
    from pydsh.session.session import _validate_lossless_json

    message = create_assistant_message([TextBlock("hi")], provider="p", model="m")
    encoded = encode_payload({"turn": 1, "step": 1, "message": message, "usage": None})
    _validate_lossless_json(encoded)  # raises if not round-trippable


def test_encoded_message_reaches_the_session_log():
    """End-to-end: a Message survives append -> derive_messages -> decode."""
    from pydsh.session.session import Session

    class _Ctx:
        def emit(self, *args, **kwargs):
            pass

    message = create_assistant_message([TextBlock("hello")], provider="p", model="m")
    session = Session(_Ctx(), id="s1")
    session.append(
        "assistant/message",
        encode_payload({"turn": 1, "step": 1, "message": message, "usage": None}),
    )
    derived = session.derive_messages()
    assert decode_payload(derived[0]) == message


# --------------------------------------------------------------------------- #
# Encoding a dataclass the vocabulary does not know (spec 03, Requirement 8)
# --------------------------------------------------------------------------- #
def test_a_nested_block_keeps_its_tag_through_an_unknown_dataclass():
    """R8.1 — the defect: `asdict` recursed into nested dataclasses itself.

    A StreamChunk carrying a TextBlock reached the log as a bare
    ``{"text": ...}`` with the vocabulary tag gone, so the decode could not
    restore the block — which is exactly the token-level replay fidelity the
    `assistant/chunk` event exists to provide.
    """
    from pydsh.llm import ChunkType, StreamChunk

    chunk = StreamChunk(
        type=ChunkType.BLOCK_END, index=0, block=TextBlock("assembled")
    )
    encoded = encode_payload(chunk)
    assert encoded["block"] == {"__block__": "text", "text": "assembled"}


def test_a_stream_chunk_round_trips_with_its_block_intact():
    """R8.2 — the replayed chunk holds the same block the live one did."""
    from pydsh.llm import ChunkType, StreamChunk

    chunk = StreamChunk(
        type=ChunkType.BLOCK_END, index=1, block=ToolCallBlock(
            id="c1", name="read", arguments='{"path": "a"}'
        )
    )
    restored = decode_payload(encode_payload(chunk))
    assert restored["block"] == ToolCallBlock(
        id="c1", name="read", arguments='{"path": "a"}'
    )
    assert restored["type"] == "block-end"


def test_an_encoded_chunk_is_accepted_by_the_session_log():
    """The str-Enum tag and every field must satisfy the lossless validator."""
    from pydsh.llm import ChunkType, StreamChunk
    from pydsh.session.session import _validate_lossless_json

    chunk = StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text="hi")
    _validate_lossless_json(encode_payload({"turn": 1, "step": 1, "chunk": chunk}))
