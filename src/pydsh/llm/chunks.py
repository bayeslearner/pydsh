"""The token-level streaming protocol and the request structure.

A provider's answer arrives as a sequence of :class:`StreamChunk` frames, each
tagged by :class:`ChunkType`. The tag says which of the payload fields is
meaningful — the dataclass is a tagged union flattened into one shape, matching
the reference rather than splitting into a class per tag.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class ChunkType(str, Enum):
    """The kind of one frame on an LLM stream."""

    BLOCK_START = "block-start"
    TEXT_DELTA = "text-delta"
    REASONING_DELTA = "reasoning-delta"
    TOOL_CALL_DELTA = "tool-call-delta"
    BLOCK_END = "block-end"
    USAGE = "usage"
    FINISH = "finish"


#: The chunk kinds that carry visible model output. The first one to arrive
#: marks the time-to-first-token boundary.
TOKEN_DELTA_TYPES = (
    ChunkType.TEXT_DELTA,
    ChunkType.REASONING_DELTA,
    ChunkType.TOOL_CALL_DELTA,
)


def is_token_delta(chunk_type: ChunkType) -> bool:
    """True when a chunk carries visible model output.

    Lives here rather than with the message vocabulary: the predicate is about
    chunks, and putting it in ``message`` would make the vocabulary depend on
    the LLM seam.
    """
    return chunk_type in TOKEN_DELTA_TYPES


@dataclass
class StreamChunk:
    """One frame on an LLM stream, discriminated by :attr:`type`."""

    type: ChunkType
    index: Optional[int] = None
    block_type: Optional[str] = None
    text: Optional[str] = None
    reasoning: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_call_name: Optional[str] = None
    arguments_delta: Optional[str] = None
    block: Optional[Any] = None
    usage: Optional[dict] = None
    finish: Optional[dict] = None


@dataclass
class GenerateOptions:
    """One model request, as handed to an adapter.

    ``provider`` and ``model`` are resolved by the seam's call-config merge
    before an adapter ever sees this, so an adapter may trust them.
    """

    provider: str
    model: str
    messages: list[Any]
    system: Optional[str] = None
    tools: Optional[list[dict]] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stop: Optional[list[str]] = None
    reasoning_effort: Optional[str] = None
    signal: Optional[Any] = None
    session_id: Optional[str] = None
    purpose: Optional[str] = None


__all__ = [
    "ChunkType",
    "StreamChunk",
    "GenerateOptions",
    "is_token_delta",
    "TOKEN_DELTA_TYPES",
]
