"""The block assembler — Requirement 4.

Pure logic over a frame stream. The cases that matter are the ugly ones: a
provider that interleaves blocks, one that omits the closing frame, one that
opens a tool call and sends nothing.
"""

from __future__ import annotations

from pydsh.agent import BlockAssembler
from pydsh.llm import ChunkType, StreamChunk
from pydsh.message import ReasoningBlock, TextBlock, ToolCallBlock


def push_all(chunks: list[StreamChunk]) -> BlockAssembler:
    assembler = BlockAssembler()
    for chunk in chunks:
        assembler.push(chunk)
    assembler.finalize()
    return assembler


def text(index: int, value: str) -> StreamChunk:
    return StreamChunk(type=ChunkType.TEXT_DELTA, index=index, text=value)


def finish(kind: str = "stop") -> StreamChunk:
    return StreamChunk(type=ChunkType.FINISH, finish={"kind": kind})


# --------------------------------------------------------------------------- #
# Accumulation (R4.1, R4.2)
# --------------------------------------------------------------------------- #
def test_text_deltas_accumulate():
    assembler = push_all([text(0, "hel"), text(0, "lo"), finish()])
    assert assembler.blocks == [TextBlock("hello")]


def test_reasoning_accumulates_separately_from_text():
    assembler = push_all(
        [
            StreamChunk(type=ChunkType.REASONING_DELTA, index=0, reasoning="thinking"),
            text(1, "answer"),
            finish(),
        ]
    )
    assert assembler.blocks == [ReasoningBlock("thinking"), TextBlock("answer")]


def test_tool_call_deltas_accumulate_by_index():
    assembler = push_all(
        [
            StreamChunk(
                type=ChunkType.TOOL_CALL_DELTA, index=0,
                tool_call_id="c1", tool_call_name="read", arguments_delta='{"pa',
            ),
            StreamChunk(type=ChunkType.TOOL_CALL_DELTA, index=0, arguments_delta='th": 1}'),
            finish("tool-calls"),
        ]
    )
    assert assembler.blocks == [ToolCallBlock(id="c1", name="read", arguments='{"path": 1}')]


def test_two_tool_calls_keep_their_order():
    """The loop back-fills results in this order, so it has to be the model's."""
    assembler = push_all(
        [
            StreamChunk(type=ChunkType.TOOL_CALL_DELTA, index=0,
                        tool_call_id="c1", tool_call_name="first", arguments_delta="{}"),
            StreamChunk(type=ChunkType.TOOL_CALL_DELTA, index=1,
                        tool_call_id="c2", tool_call_name="second", arguments_delta="{}"),
            finish("tool-calls"),
        ]
    )
    assert [b.name for b in assembler.tool_calls()] == ["first", "second"]


def test_blocks_come_out_in_the_order_they_opened():
    assembler = push_all(
        [
            text(0, "before"),
            StreamChunk(type=ChunkType.BLOCK_END, index=0, block_type="text"),
            StreamChunk(type=ChunkType.TOOL_CALL_DELTA, index=1,
                        tool_call_id="c1", tool_call_name="run", arguments_delta="{}"),
            finish("tool-calls"),
        ]
    )
    assert [type(b).__name__ for b in assembler.blocks] == ["TextBlock", "ToolCallBlock"]


# --------------------------------------------------------------------------- #
# Closing (R4.3–R4.6)
# --------------------------------------------------------------------------- #
def test_a_provider_supplied_block_wins_over_the_accumulation():
    """R4.3 — the provider knows its own normalization."""
    assembler = push_all(
        [
            text(0, "raw"),
            StreamChunk(type=ChunkType.BLOCK_END, index=0, block=TextBlock("normalized")),
            finish(),
        ]
    )
    assert assembler.blocks == [TextBlock("normalized")]


def test_a_block_end_without_a_block_still_closes_the_accumulation():
    """R4.3 — the frame is never merely dropped."""
    assembler = push_all(
        [text(0, "kept"), StreamChunk(type=ChunkType.BLOCK_END, index=0, block_type="text"),
         finish()]
    )
    assert assembler.blocks == [TextBlock("kept")]


def test_usage_and_finish_are_recorded():
    """R4.4, R4.5"""
    assembler = push_all(
        [
            text(0, "hi"),
            StreamChunk(type=ChunkType.USAGE, usage={"input": 10, "output": 2}),
            finish("max-tokens"),
        ]
    )
    assert assembler.usage == {"input": 10, "output": 2}
    assert assembler.finish == {"kind": "max-tokens"}


def test_the_finish_frame_closes_open_blocks():
    """R4.5 — a provider that never sends block-end still yields a message."""
    assembler = BlockAssembler()
    assembler.push(text(0, "unclosed"))
    assembler.push(finish())
    assert assembler.blocks == [TextBlock("unclosed")]


def test_a_stream_that_ends_without_a_finish_is_still_assembled():
    """R4.6 — a dropped connection must not lose the partial answer."""
    assembler = BlockAssembler()
    assembler.push(text(0, "truncated"))
    assembler.finalize()
    assert assembler.blocks == [TextBlock("truncated")]
    assert assembler.finish == {"kind": "stop"}


def test_finalize_is_idempotent():
    """The loop calls it again after a finish frame already did."""
    assembler = BlockAssembler()
    assembler.push(text(0, "once"))
    assembler.push(finish())
    assembler.finalize()
    assembler.finalize()
    assert assembler.blocks == [TextBlock("once")]


def test_an_empty_tool_call_is_dropped():
    """A call opened but never given an id, name, or arguments is a shell.

    Sending it on as a call to the tool named "" only produces a confusing
    error for the model to read.
    """
    assembler = push_all(
        [
            StreamChunk(type=ChunkType.BLOCK_START, index=0, block_type="tool-call"),
            finish("tool-calls"),
        ]
    )
    assert assembler.tool_calls() == []


def test_an_empty_stream_assembles_to_nothing():
    assembler = push_all([finish()])
    assert assembler.blocks == []
    assert assembler.usage is None
