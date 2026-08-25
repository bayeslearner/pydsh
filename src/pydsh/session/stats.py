"""``session_stats`` — how much work a conversation has been, folded from its log.

Turns, steps, and the four timings an operator actually asks about: how long
the model took, how long its tools took, how long until the first token
appeared, and how fast it decoded after that.

Everything here reads the **encoded** payloads the log stores, not live
vocabulary objects. The reference's unit calls ``is_token_delta(chunk)`` on a
``StreamChunk`` and pulls a ``ToolResultBlock`` off a ``Message``; in this port
those reached the log through ``encode_payload`` and come back as tagged dicts.
Transcribing the reference would make every statistic zero on a real
conversation — quietly, since zero is a plausible number. This is the same
defect spec 02's token meter was written to avoid.

Decoding per event would fix it too, but this fold runs on *every* append, and
paying a decode there to read one tag is the wrong cost.
"""

from __future__ import annotations

from typing import Any, Optional

from plugkit import Service

from ..llm.chunks import TOKEN_DELTA_TYPES
from .projection import ProjectionDefinition

#: The key this unit owns in a snapshot.
SESSION_STATS_KEY = "session_stats"

#: The totals the view exposes. State carries more (the open step, the calls
#: awaiting results); those are bookkeeping and do not leave.
TOTALS = (
    "turns",
    "steps",
    "llm_ms",
    "tool_ms",
    "ttft_ms",
    "ttft_steps",
    "decode_ms",
    "decode_tokens",
)

#: Chunk tags that carry visible model output, as they appear once encoded.
#: `ChunkType` is a str-Enum, so an encoded chunk's `type` is its value.
_TOKEN_DELTA_TAGS = frozenset(chunk_type.value for chunk_type in TOKEN_DELTA_TYPES)


def _is_token_delta(chunk: Any) -> bool:
    """Whether an encoded chunk carried visible output."""
    if isinstance(chunk, dict):
        return chunk.get("type") in _TOKEN_DELTA_TAGS
    return getattr(chunk, "type", None) in TOKEN_DELTA_TYPES


def _output_tokens(usage: Any) -> Optional[int]:
    """The output-token count a provider reported, if it reported one."""
    if not isinstance(usage, dict):
        return None
    for field in ("output_tokens", "output", "completion_tokens"):
        value = usage.get(field)
        if isinstance(value, (int, float)):
            return int(value)
    return None


def _result_call_id(message: Any) -> Optional[str]:
    """The call id a tool-result message answers, read off the encoded form."""
    if isinstance(message, dict):
        body = message.get("__msg__")
        blocks = body.get("content", []) if isinstance(body, dict) else []
        for block in blocks:
            if isinstance(block, dict) and block.get("__block__") == "tool-result":
                return block.get("tool_call_id")
        return None
    for block in getattr(message, "content", ()):  # a live Message
        call_id = getattr(block, "tool_call_id", None)
        if call_id is not None:
            return call_id
    return None


def _init() -> dict:
    return {
        "turns": 0,
        "steps": 0,
        "llm_ms": 0.0,
        "tool_ms": 0.0,
        "ttft_ms": 0.0,
        "ttft_steps": 0,
        "decode_ms": 0.0,
        "decode_tokens": 0,
        # Bookkeeping, not part of the view.
        "last_turn": None,
        "open_step": None,
        "pending_calls": {},
    }


def _apply(state: dict, event: Any) -> dict:
    """The transition. Returns the *same* state for anything it ignores."""
    kind = event.type

    if kind == "step/start":
        return {
            **state,
            "open_step": {
                "turn": event.data["turn"],
                "step": event.data["step"],
                "started_at": event.time,
                "first_token_at": None,
            },
        }

    if kind == "assistant/chunk":
        open_step = state["open_step"]
        if not _is_this_step(open_step, event):
            return state
        if open_step["first_token_at"] is not None:
            return state
        if not _is_token_delta(event.data.get("chunk")):
            return state
        return {
            **state,
            "open_step": {**open_step, "first_token_at": event.time},
        }

    if kind == "assistant/message":
        open_step = state["open_step"]
        if not _is_this_step(open_step, event):
            return state
        # Exactly one assembled message per step, and closing the step here is
        # what stops a defensive duplicate from counting the time twice.
        nxt = {
            **state,
            "llm_ms": state["llm_ms"] + max(0.0, event.time - open_step["started_at"]),
            "open_step": None,
        }
        first_token_at = open_step["first_token_at"]
        if first_token_at is not None:
            nxt["ttft_ms"] += max(0.0, first_token_at - open_step["started_at"])
            nxt["ttft_steps"] += 1
            output_tokens = _output_tokens(event.data.get("usage"))
            if output_tokens is not None:
                nxt["decode_ms"] += max(0.0, event.time - first_token_at)
                nxt["decode_tokens"] += output_tokens
        return nxt

    if kind == "tool/call":
        return {
            **state,
            "pending_calls": {**state["pending_calls"], event.data["callId"]: event.time},
        }

    if kind == "tool/result":
        call_id = _result_call_id(event.data.get("message"))
        dispatched = state["pending_calls"].get(call_id) if call_id else None
        if dispatched is None:
            return state
        remaining = {k: v for k, v in state["pending_calls"].items() if k != call_id}
        return {
            **state,
            "tool_ms": state["tool_ms"] + max(0.0, event.time - dispatched),
            "pending_calls": remaining,
        }

    if kind == "step/end":
        turn = event.data["turn"]
        return {
            **state,
            # A turn is counted once however many steps it took.
            "turns": state["turns"] if state["last_turn"] == turn else state["turns"] + 1,
            "steps": state["steps"] + 1,
            "last_turn": turn,
            "open_step": None,
        }

    if kind == "turn/end":
        # A result always lands inside its own turn. Calls left unpaired by a
        # cancelled or failed turn are dropped here rather than accumulating
        # for the life of the session.
        return state if not state["pending_calls"] else {**state, "pending_calls": {}}

    return state


def _is_this_step(open_step: Optional[dict], event: Any) -> bool:
    """Whether an event belongs to the step currently open."""
    return (
        open_step is not None
        and open_step["turn"] == event.data.get("turn")
        and open_step["step"] == event.data.get("step")
    )


def _view(state: dict) -> dict:
    return {name: state[name] for name in TOTALS}


#: The unit, ready to register on ``ctx.session_projections``.
session_stats_definition = ProjectionDefinition(
    key=SESSION_STATS_KEY,
    init=_init,
    apply=_apply,
    view=_view,
    state_version=1,
)


class SessionStats(Service):
    """Registers the ``session_stats`` unit for as long as it is mounted."""

    provide = "session_stats"
    inject = ["session_projections"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        dispose = ctx.session_projections.register(session_stats_definition)
        ctx.effect(lambda: dispose)

    def of(self, session: Any) -> dict:
        """This session's totals, right now."""
        return self.ctx.session_projections.snapshot(session)["values"][
            SESSION_STATS_KEY
        ]


__all__ = [
    "SessionStats",
    "session_stats_definition",
    "SESSION_STATS_KEY",
    "TOTALS",
]
