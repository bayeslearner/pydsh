"""Token estimation and surface measurement — Requirement 6."""

from __future__ import annotations

import pytest

from plugkit import Context

from pydsh.llm import TokenMeter, estimate_text
from pydsh.message import (
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    create_assistant_message,
    create_user_message,
    encode_payload,
)
from pydsh.session import Session, SessionStore

# asyncio_mode = "auto" (pyproject) picks up the async tests; a module-level
# asyncio mark would also fire on the sync estimator tests below.


async def mounted() -> Context:
    root = Context()
    await root.plugin(TokenMeter)
    return root


# --------------------------------------------------------------------------- #
# Estimation
# --------------------------------------------------------------------------- #
def test_empty_text_is_zero():
    assert estimate_text("") == 0


def test_non_empty_text_is_never_zero():
    assert estimate_text("a") == 1


def test_ascii_uses_the_characters_per_token_ratio():
    assert estimate_text("a" * 40) == 10


def test_cjk_counts_one_token_per_character():
    assert estimate_text("会话日志") == 4


def test_mixed_script_adds_both_parts():
    # 4 CJK + 8 ascii -> 4 + 2
    assert estimate_text("会话日志" + "a" * 8) == 6


async def test_estimate_message_reads_every_text_block():
    root = await mounted()
    message = create_user_message([TextBlock("a" * 20), TextBlock("b" * 20)])
    assert root.token_meter.estimate_message(message) == 10


async def test_estimate_counts_tool_call_arguments():
    root = await mounted()
    message = create_assistant_message(
        [ToolCallBlock(id="c1", name="fs", arguments="x" * 16)]
    )
    assert root.token_meter.estimate_message(message) == 4


async def test_estimate_recurses_into_tool_results():
    """A nested tool result is priced, not silently zero."""
    root = await mounted()
    message = create_user_message(
        [ToolResultBlock(tool_call_id="c1", content=(TextBlock("y" * 20),), is_error=False)]
    )
    assert root.token_meter.estimate_message(message) == 5


async def test_estimate_handles_an_encoded_message():
    """What the session log hands back is encoded, not a live Message."""
    root = await mounted()
    message = create_user_message([TextBlock("a" * 20)])
    assert root.token_meter.estimate_message(encode_payload(message)) == 5


async def test_estimate_of_none_is_zero():
    root = await mounted()
    assert root.token_meter.estimate_message(None) == 0


# --------------------------------------------------------------------------- #
# Surface measurement
# --------------------------------------------------------------------------- #
async def test_measure_empty_session():
    root = Context()
    await root.plugin(TokenMeter)
    await root.plugin(SessionStore)
    session = root.sessions.create("s")
    assert root.token_meter.measure(session) == {"nodes": [], "total_tokens": 0}


async def test_measure_one_entry_per_surface_node():
    root = Context()
    await root.plugin(TokenMeter)
    await root.plugin(SessionStore)
    session = root.sessions.create("s")
    session.append("user/message", encode_payload(create_user_message([TextBlock("a" * 20)])))
    session.append("turn/start", {"turn": 1})  # log-only, not on the surface
    session.append("user/message", encode_payload(create_user_message([TextBlock("b" * 40)])))

    measured = root.token_meter.measure(session)
    assert [n["seq"] for n in measured["nodes"]] == [1, 3]
    assert [n["tokens"] for n in measured["nodes"]] == [5, 10]
    assert measured["total_tokens"] == 15


async def test_measure_raises_on_a_corrupt_surface():
    """Requirement 6.3 — a broken surface must not price as zero."""
    root = await mounted()

    class _Ctx:
        def emit(self, *a, **k):
            pass

    session = Session(_Ctx(), id="s")
    session.append("user/message", {"content": [], "role": "user", "source": {}})
    # Corrupt the projection: point at a seq the log does not have.
    session._surface_nodes.append(99)

    with pytest.raises(RuntimeError, match="no matching log event"):
        root.token_meter.measure(session)
