"""The shared conversation vocabulary.

Content blocks, messages, and their provenance — plus the lossless-JSON
encoding that lets a message reach spec 01's session log. Every seam (session,
llm, agent) speaks this vocabulary; it speaks none of theirs.
"""

from .blocks import (
    ContentBlock,
    Message,
    MessageSource,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    as_text,
    create_assistant_message,
    create_user_message,
    new_id,
)
from .payload import PayloadDecodeError, decode_payload, encode_payload

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
    "encode_payload",
    "decode_payload",
    "PayloadDecodeError",
]
