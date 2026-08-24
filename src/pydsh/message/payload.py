"""Lossless-JSON encoding for values carrying the message vocabulary.

Spec 01's session log accepts only values that survive a JSON round-trip
byte-identically. A :class:`~pydsh.message.blocks.Message` is not such a value,
so anything holding one passes through :func:`encode_payload` on the way to the
log and :func:`decode_payload` on the way back.

The encoding is a tag scheme: each vocabulary type becomes a dict carrying a
reserved key (``__msg__``, ``__block__``, ``__source__``) that the decoder
dispatches on. Ordinary dicts, lists, and scalars pass through untouched.

One normalization is unavoidable: JSON has a single sequence type, so a tuple
encodes to a list and decodes back as a list. The vocabulary's own tuple fields
(``Message.content``, ``ToolResultBlock.content``) are restored as tuples,
because the decoder knows their shape; a bare tuple handed in as free-form
payload does not survive as a tuple.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Any

from .blocks import (
    Message,
    MessageSource,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)


class PayloadDecodeError(ValueError):
    """A payload carried a tag the vocabulary does not define."""


def _encode(value: Any) -> Any:
    """Recursively tag vocabulary types; pass everything else through."""
    if isinstance(value, Message):
        return {
            "__msg__": {
                "id": value.id,
                "role": value.role,
                "content": [_encode(b) for b in value.content],
                "source": _encode(value.source),
            }
        }
    if isinstance(value, TextBlock):
        return {"__block__": "text", "text": value.text}
    if isinstance(value, ReasoningBlock):
        return {"__block__": "reasoning", "text": value.text}
    if isinstance(value, ToolCallBlock):
        return {
            "__block__": "tool-call",
            "id": value.id,
            "name": value.name,
            "arguments": value.arguments,
        }
    if isinstance(value, ToolResultBlock):
        return {
            "__block__": "tool-result",
            "tool_call_id": value.tool_call_id,
            "content": [_encode(b) for b in value.content],
            "is_error": value.is_error,
        }
    if isinstance(value, MessageSource):
        # Every field is encoded, including the goal-attribution trio. The
        # reference drops those three, which silently loses a goal message's
        # round/revision on reload.
        return {
            "__source__": value.kind,
            "plugin": value.plugin,
            "form": value.form,
            "provider": value.provider,
            "model": value.model,
            "goal_id": value.goal_id,
            "revision": value.revision,
            "round": value.round,
        }
    if isinstance(value, (tuple, list)):
        return [_encode(x) for x in value]
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if is_dataclass(value):
        # A plain dataclass the vocabulary doesn't know (e.g. StreamChunk):
        # flatten to a dict, then encode whatever it holds. Field-by-field
        # rather than `dataclasses.asdict`, because asdict recurses *itself*
        # into nested dataclasses — a StreamChunk carrying a TextBlock would
        # reach the log as a bare {"text": ...} with its tag gone, so the
        # decode could not restore the block and `assistant/chunk` would not
        # have the replay fidelity it exists for. Walking one level and
        # recursing through _encode keeps every nested tag.
        return {f.name: _encode(getattr(value, f.name)) for f in fields(value)}
    return value


def _decode_block(value: dict) -> Any:
    """Rebuild one content block from its tag."""
    kind = value["__block__"]
    if kind == "text":
        return TextBlock(value["text"])
    if kind == "reasoning":
        return ReasoningBlock(value["text"])
    if kind == "tool-call":
        return ToolCallBlock(
            id=value["id"], name=value["name"], arguments=value["arguments"]
        )
    if kind == "tool-result":
        return ToolResultBlock(
            tool_call_id=value["tool_call_id"],
            content=tuple(_decode(b) for b in value["content"]),
            is_error=value["is_error"],
        )
    raise PayloadDecodeError(f"unknown content block tag {kind!r}")


def _decode(value: Any) -> Any:
    """Inverse of :func:`_encode`."""
    if isinstance(value, dict):
        if "__msg__" in value:
            m = value["__msg__"]
            return Message(
                id=m["id"],
                role=m["role"],
                content=tuple(_decode(b) for b in m["content"]),
                source=_decode(m["source"]),
            )
        if "__block__" in value:
            return _decode_block(value)
        if "__source__" in value:
            return MessageSource(
                kind=value["__source__"],
                plugin=value.get("plugin", ""),
                form=value.get("form", ""),
                provider=value.get("provider", ""),
                model=value.get("model", ""),
                goal_id=value.get("goal_id", ""),
                revision=value.get("revision"),
                round=value.get("round"),
            )
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(x) for x in value]
    return value


def encode_payload(value: Any) -> Any:
    """Encode an event payload into a JSON-safe structure (the write path)."""
    return _encode(value)


def decode_payload(value: Any) -> Any:
    """Restore an event payload encoded by :func:`encode_payload` (read path)."""
    return _decode(value)


__all__ = ["encode_payload", "decode_payload", "PayloadDecodeError"]
