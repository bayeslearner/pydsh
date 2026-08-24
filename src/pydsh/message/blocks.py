"""Content blocks and messages — the shared conversation vocabulary.

A content block is a tagged union of four kinds: text, reasoning, tool call,
and tool result. A :class:`Message` is an immutable record carrying a role and
a :class:`MessageSource` saying who produced it.

This module imports nothing from the LLM seam. The vocabulary is the more
stable layer — the session log, the agent loop, and every adapter all consume
it, so a dependency on the seam would drag the seam into all of them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional, Union


def new_id() -> str:
    """A stable identifier for one message."""
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- #
# Content blocks
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TextBlock:
    """Plain model-visible text."""

    text: str


@dataclass(frozen=True)
class ReasoningBlock:
    """The model's thinking, kept distinct from its answer."""

    text: str


@dataclass(frozen=True)
class ToolCallBlock:
    """One tool invocation the model asked for.

    ``arguments`` stays the raw JSON string the model produced — parsing it is
    the tool layer's job, and keeping it raw means a malformed call survives
    round-tripping instead of being lost at the vocabulary boundary.
    """

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ToolResultBlock:
    """The outcome of a tool call, addressed back to it by ``tool_call_id``."""

    tool_call_id: str
    content: tuple
    is_error: bool


ContentBlock = Union[TextBlock, ReasoningBlock, ToolCallBlock, ToolResultBlock]


def as_text(content: tuple) -> str:
    """Join the text blocks of a content tuple, ignoring every other kind."""
    return "".join(b.text for b in content if isinstance(b, TextBlock))


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MessageSource:
    """Who produced a message.

    ``kind`` is one of ``user`` / ``model`` / ``tool`` / ``plugin`` / ``goal``.
    The remaining fields are kind-specific and default to empty rather than
    ``None`` so that equality and serialization stay total.
    """

    kind: str
    plugin: str = ""
    form: str = ""
    provider: str = ""
    model: str = ""
    goal_id: str = ""
    revision: Optional[int] = None
    round: Optional[int] = None


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Message:
    """One immutable message, shared across delivery, history, and requests."""

    id: str
    role: str
    content: tuple
    source: MessageSource


def create_user_message(
    content: list[ContentBlock], source: MessageSource | None = None
) -> Message:
    """Build a ``user``-role message, defaulting to a user source."""
    return Message(
        id=new_id(),
        role="user",
        content=tuple(content),
        source=source or MessageSource("user"),
    )


def create_assistant_message(
    content: list[ContentBlock], provider: str = "", model: str = ""
) -> Message:
    """Build an ``assistant``-role message attributed to a provider/model."""
    return Message(
        id=new_id(),
        role="assistant",
        content=tuple(content),
        source=MessageSource("model", provider=provider, model=model),
    )


__all__ = [
    "TextBlock",
    "ReasoningBlock",
    "ToolCallBlock",
    "ToolResultBlock",
    "ContentBlock",
    "MessageSource",
    "Message",
    "as_text",
    "new_id",
    "create_user_message",
    "create_assistant_message",
]
