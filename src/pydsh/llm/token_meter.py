"""``ctx.token_meter`` — one estimator, so pressure is measured consistently.

Compaction needs a single answer to "how big is this conversation". The
reference uses a dedicated token-meter package; this port ships the zero-
dependency heuristic: CJK codepoints count as one token each, everything else
at a fixed characters-per-token ratio.

The estimate is deliberately approximate — it exists to *rank and budget*, not
to bill. What it must never do is quietly price a corrupt surface at zero, so
:meth:`TokenMeter.measure` raises when a surface node has no matching event.
"""

from __future__ import annotations

import math
import re
from typing import Any

from plugkit import Service

from ..message import ToolResultBlock, decode_payload

# CJK ideographs run roughly one token per character.
_CJK = re.compile(r"[一-鿿]")

#: Non-CJK characters per token — the industry rule of thumb.
CHARS_PER_TOKEN = 4.0


def estimate_text(text: str) -> int:
    """Estimate the token count of a string.

    Empty text is zero; anything non-empty is at least one, because no real
    tokenizer emits nothing for a non-empty string.
    """
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return max(1, math.ceil(cjk + other / CHARS_PER_TOKEN))


def _block_text(block: Any) -> str:
    """The text a single content block contributes to an estimate."""
    if isinstance(block, ToolResultBlock):
        # A tool result nests its own blocks — recurse, don't price it at zero.
        return "".join(_block_text(inner) for inner in block.content)
    text = getattr(block, "text", None)
    if text:
        return text
    arguments = getattr(block, "arguments", None)
    if arguments:
        return arguments
    return ""


def message_text(message: Any) -> str:
    """Extract the estimable text of a message, a block, or a content tuple."""
    if message is None:
        return ""
    if isinstance(message, (tuple, list)):
        return "".join(message_text(item) for item in message)
    content = getattr(message, "content", None)
    if content is not None and not isinstance(message, ToolResultBlock):
        return "".join(_block_text(block) for block in content)
    return _block_text(message)


class TokenMeter(Service):
    """The ``token_meter`` service."""

    provide = "token_meter"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)

    def estimate_text(self, text: str) -> int:
        """Estimate one string."""
        return estimate_text(text)

    def estimate_message(self, message: Any) -> int:
        """Estimate one message, encoded or live.

        Messages reach the session log through ``encode_payload``, so what
        ``derive_event_message`` hands back is a tagged dict, not a ``Message``.
        Decoding first is what keeps a real conversation from being priced at
        zero; for a plain dict or a live ``Message``, decoding is the identity.
        """
        return estimate_text(message_text(decode_payload(message)))

    def measure(self, session: Any) -> dict:
        """Measure a session's surface: one entry per node, plus the total.

        :raises RuntimeError: a surface node has no matching log event — the
            surface is corrupt, and silently returning zero would hide it.
        """
        nodes: list[dict] = []
        total = 0
        events = session.events
        for seq in session.surface_nodes:
            # Surface seqs are 1-based; the log is a 0-based list.
            event = events[seq - 1] if 1 <= seq <= len(events) else None
            if event is None or event.seq != seq:
                raise RuntimeError(
                    f"token meter: surface seq {seq} has no matching log event"
                )
            message = session.derive_event_message(event)
            tokens = self.estimate_message(message) if message is not None else 0
            nodes.append({"seq": seq, "tokens": tokens})
            total += tokens
        return {"nodes": nodes, "total_tokens": total}


__all__ = ["TokenMeter", "estimate_text", "message_text", "CHARS_PER_TOKEN"]
