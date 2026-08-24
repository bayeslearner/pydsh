"""Folding a stream of frames into a finished assistant message.

A provider answers in :class:`~pydsh.llm.chunks.StreamChunk` frames: deltas of
text, of reasoning, of tool-call arguments, interleaved and identified by a
block index. The assembler is the fold that turns that back into whole content
blocks, plus the usage and finish reason the loop needs to decide what happens
next.

Two things it must get right:

- **Order.** Blocks come out in the order the provider opened them, which is
  the order the model meant them in. Tool calls in particular must keep their
  order, because the loop back-fills their results in that same order.
- **Truncation.** A stream can stop mid-block — a dropped connection, a
  cancelled request, a provider that omits the closing frame.
  :meth:`finalize` force-closes whatever is still open, so the loop writes a
  well-formed (if short) message rather than losing the partial answer.
"""

from __future__ import annotations

from typing import Any, Optional

from ..llm.chunks import ChunkType, StreamChunk
from ..message.blocks import ReasoningBlock, TextBlock, ToolCallBlock

#: The finish a stream is assumed to have had when it never said.
DEFAULT_FINISH: dict = {"kind": "stop"}


class _OpenBlock:
    """Text accumulating under one block index, in arrival order."""

    __slots__ = ("index", "order", "text")

    def __init__(self, index: int, order: int) -> None:
        self.index = index
        self.order = order
        self.text = ""


class _OpenToolCall:
    """A tool call accumulating id, name, and argument text."""

    __slots__ = ("index", "order", "id", "name", "arguments")

    def __init__(self, index: int, order: int) -> None:
        self.index = index
        self.order = order
        self.id = ""
        self.name = ""
        self.arguments = ""

    @property
    def started(self) -> bool:
        """Whether anything at all arrived for this call."""
        return bool(self.id or self.name or self.arguments)


class BlockAssembler:
    """Consumes :class:`StreamChunk` frames; produces blocks, usage, finish."""

    def __init__(self) -> None:
        self._text: Optional[_OpenBlock] = None
        self._reasoning: Optional[_OpenBlock] = None
        self._tool_calls: dict[int, _OpenToolCall] = {}
        self._next_order = 0
        # (order, block) pairs. Kept ordered internally rather than sorted into
        # `blocks` in place, so finalize() stays idempotent — the loop calls it
        # again when a stream ends without a finish frame.
        self._closed: list[tuple[int, Any]] = []
        self.usage: Optional[dict] = None
        self.finish: dict = dict(DEFAULT_FINISH)

    @property
    def blocks(self) -> list[Any]:
        """The closed blocks, in the order the provider opened them."""
        return [block for _order, block in sorted(self._closed, key=lambda p: p[0])]

    def _order(self) -> int:
        order = self._next_order
        self._next_order += 1
        return order

    def push(self, chunk: StreamChunk) -> None:
        """Take one frame."""
        kind = chunk.type
        if kind == ChunkType.BLOCK_START:
            self._start(chunk)
        elif kind == ChunkType.TEXT_DELTA:
            self._text = self._text or _OpenBlock(chunk.index or 0, self._order())
            self._text.text += chunk.text or ""
        elif kind == ChunkType.REASONING_DELTA:
            self._reasoning = self._reasoning or _OpenBlock(chunk.index or 0, self._order())
            self._reasoning.text += chunk.reasoning or ""
        elif kind == ChunkType.TOOL_CALL_DELTA:
            self._tool_delta(chunk)
        elif kind == ChunkType.BLOCK_END:
            self._end(chunk)
        elif kind == ChunkType.USAGE:
            self.usage = chunk.usage
        elif kind == ChunkType.FINISH:
            self.finish = chunk.finish or dict(DEFAULT_FINISH)
            self.finalize()

    def _start(self, chunk: StreamChunk) -> None:
        block_type = chunk.block_type
        index = chunk.index or 0
        if block_type == "text":
            self._text = self._text or _OpenBlock(index, self._order())
        elif block_type == "reasoning":
            self._reasoning = self._reasoning or _OpenBlock(index, self._order())
        elif block_type == "tool-call":
            self._open_tool_call(index)

    def _open_tool_call(self, index: int) -> _OpenToolCall:
        call = self._tool_calls.get(index)
        if call is None:
            call = _OpenToolCall(index, self._order())
            self._tool_calls[index] = call
        return call

    def _tool_delta(self, chunk: StreamChunk) -> None:
        call = self._open_tool_call(chunk.index or 0)
        if chunk.tool_call_id is not None:
            call.id = chunk.tool_call_id
        if chunk.tool_call_name is not None:
            call.name = chunk.tool_call_name
        call.arguments += chunk.arguments_delta or ""

    def _end(self, chunk: StreamChunk) -> None:
        """Close a block the provider handed back whole.

        An adapter may send the finished block on the closing frame. When it
        does, that value wins over what was accumulated — the provider knows
        its own normalization. When it does not, the accumulated block is
        closed instead, so the frame is never merely dropped.
        """
        block = chunk.block
        if isinstance(block, TextBlock):
            self._close_text(block)
        elif isinstance(block, ReasoningBlock):
            self._close_reasoning(block)
        elif isinstance(block, ToolCallBlock):
            self._close_tool_call(chunk.index or 0, block)
        else:
            self._close_by_type(chunk)

    def _close_by_type(self, chunk: StreamChunk) -> None:
        block_type = chunk.block_type
        if block_type == "text":
            self._close_text(None)
        elif block_type == "reasoning":
            self._close_reasoning(None)
        elif block_type == "tool-call":
            self._close_tool_call(chunk.index or 0, None)

    def _close_text(self, block: Optional[TextBlock]) -> None:
        open_block, self._text = self._text, None
        if block is None and open_block is None:
            return
        order = open_block.order if open_block else self._order()
        self._closed.append((order, block or TextBlock(open_block.text)))

    def _close_reasoning(self, block: Optional[ReasoningBlock]) -> None:
        open_block, self._reasoning = self._reasoning, None
        if block is None and open_block is None:
            return
        order = open_block.order if open_block else self._order()
        self._closed.append((order, block or ReasoningBlock(open_block.text)))

    def _close_tool_call(self, index: int, block: Optional[ToolCallBlock]) -> None:
        call = self._tool_calls.pop(index, None)
        if block is None and call is None:
            return
        order = call.order if call else self._order()
        self._closed.append(
            (order, block or ToolCallBlock(id=call.id, name=call.name, arguments=call.arguments))
        )

    def finalize(self) -> None:
        """Close anything still open.

        Called on the finish frame, and again by the loop when the stream ended
        without one. Idempotent: afterwards nothing is open, so a second call
        does nothing.
        """
        if self._text is not None:
            self._close_text(None)
        if self._reasoning is not None:
            self._close_reasoning(None)
        for index in sorted(self._tool_calls):
            # A tool call opened but never given an id, a name, or any
            # arguments is an empty shell — sending it on as a call to the tool
            # named "" only produces a confusing error for the model to read.
            if self._tool_calls[index].started:
                self._close_tool_call(index, None)
        self._tool_calls.clear()

    def tool_calls(self) -> list[ToolCallBlock]:
        """The finished tool-call blocks, in the order the model asked."""
        return [b for b in self.blocks if isinstance(b, ToolCallBlock)]


__all__ = ["BlockAssembler", "DEFAULT_FINISH"]
