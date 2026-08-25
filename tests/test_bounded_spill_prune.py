"""Spilling and pruning — Requirements 5 and 6, property 3."""

from __future__ import annotations

import pytest

from plugkit import Context

from pydsh.bounded import (
    PRUNE_MARKER,
    LocalSpillStore,
    ToolResultPruner,
    encode_segment,
    resolve_config,
)
from pydsh.message import ReasoningBlock, TextBlock, ToolCallBlock

pytestmark = pytest.mark.asyncio


async def spill_store(tmp_path) -> Context:
    root = Context()
    await root.plugin(LocalSpillStore, {"root": str(tmp_path / "spill")})
    return root


async def pruner(**config) -> Context:
    root = Context()
    await root.plugin(ToolResultPruner, config)
    return root


# --------------------------------------------------------------------------- #
# Segment encoding (R5.2)
# --------------------------------------------------------------------------- #
async def test_safe_names_pass_through():
    assert encode_segment("results-2026_01.txt") == "results-2026_01.txt"


async def test_separators_are_encoded():
    assert "/" not in encode_segment("a/b/c")
    assert "\\" not in encode_segment("a\\b")


async def test_dot_dot_cannot_survive_encoding():
    """The single case this function exists for."""
    encoded = encode_segment("..")
    assert encoded != ".."
    assert "/" not in encoded


async def test_a_leading_dot_is_escaped():
    assert not encode_segment(".hidden").startswith(".")


async def test_an_empty_segment_becomes_a_placeholder():
    assert encode_segment("") == "_"


# --------------------------------------------------------------------------- #
# Spilling (R5.1, R5.3, R5.4)
# --------------------------------------------------------------------------- #
async def test_a_spill_writes_the_content_and_returns_a_locator(tmp_path):
    root = await spill_store(tmp_path)
    result = await root.spill.save_text("chat-1", "grep.txt", "a lot of output")

    assert result["bytes"] == 15
    assert "read" in result["retrieval_hint"].lower()
    from pathlib import Path

    assert Path(result["locator"]).read_text() == "a lot of output"


async def test_spills_are_scoped_per_session(tmp_path):
    root = await spill_store(tmp_path)
    first = await root.spill.save_text("chat-1", "out.txt", "one")
    second = await root.spill.save_text("chat-2", "out.txt", "two")

    assert first["locator"] != second["locator"]
    from pathlib import Path

    assert Path(first["locator"]).read_text() == "one"
    assert Path(second["locator"]).read_text() == "two"


async def test_a_traversing_session_id_cannot_escape_the_root(tmp_path):
    """I5 — both segments arrive from elsewhere and neither is trusted."""
    root = await spill_store(tmp_path)
    result = await root.spill.save_text("../../etc", "passwd", "nope")

    from pathlib import Path

    assert Path(result["locator"]).is_relative_to(tmp_path / "spill")


async def test_a_traversing_name_cannot_escape_the_root(tmp_path):
    root = await spill_store(tmp_path)
    result = await root.spill.save_text("chat-1", "../../escaped.txt", "nope")

    from pathlib import Path

    assert Path(result["locator"]).is_relative_to(tmp_path / "spill")
    assert not (tmp_path / "escaped.txt").exists()


async def test_unicode_content_round_trips(tmp_path):
    root = await spill_store(tmp_path)
    result = await root.spill.save_text("chat-1", "out.txt", "héllo ☃")

    from pathlib import Path

    assert Path(result["locator"]).read_text(encoding="utf-8") == "héllo ☃"
    assert result["bytes"] == len("héllo ☃".encode())


async def test_the_store_is_replaceable(tmp_path):
    """R5.4 — a consumer that needs spills elsewhere implements the interface."""
    from pydsh.bounded import SpillStore

    class Nowhere(SpillStore):
        async def save_text(self, session_id, suggested_name, content):
            return {"locator": "memory://nowhere", "bytes": 0, "retrieval_hint": ""}

    root = Context()
    await root.plugin(Nowhere)
    assert (await root.spill.save_text("s", "n", "x"))["locator"] == "memory://nowhere"


# --------------------------------------------------------------------------- #
# Pruner configuration (R6.2)
# --------------------------------------------------------------------------- #
async def test_the_defaults_resolve():
    config = resolve_config()
    assert config["threshold_chars"] > config["head_chars"]


async def test_an_unknown_config_key_is_rejected():
    with pytest.raises(ValueError, match="unknown config key"):
        resolve_config({"thresholdChars": 100})  # the reference's camelCase


async def test_a_non_positive_threshold_is_rejected():
    with pytest.raises(ValueError, match="threshold_chars"):
        resolve_config({"threshold_chars": 0})


async def test_a_negative_head_is_rejected():
    with pytest.raises(ValueError, match="head_chars"):
        resolve_config({"head_chars": -1})


async def test_a_config_that_could_never_shrink_is_rejected():
    """R6.2 — otherwise every prune the config asks for would fail its own check."""
    with pytest.raises(ValueError, match="must not exceed"):
        resolve_config({"threshold_chars": 100, "head_chars": 90, "tail_chars": 90})


# --------------------------------------------------------------------------- #
# Measuring and pruning (R6.3–R6.7) — property 3
# --------------------------------------------------------------------------- #
async def test_measuring_counts_text_and_ignores_the_rest():
    root = await pruner()
    blocks = [
        TextBlock("12345"),
        ToolCallBlock(id="c1", name="x", arguments="{}"),
        TextBlock("678"),
    ]
    assert root.tool_result_pruner.measure_content(blocks) == 8


async def test_content_within_budget_is_left_alone():
    root = await pruner(threshold_chars=100, head_chars=10, tail_chars=10)
    assert root.tool_result_pruner.prune_content([TextBlock("short")]) is None


async def test_the_middle_is_replaced_with_the_marker():
    root = await pruner(threshold_chars=60, head_chars=5, tail_chars=5)
    pruned = root.tool_result_pruner.prune_content([TextBlock("A" * 60 + "B" * 60)])

    text = "".join(b.text for b in pruned)
    assert text.startswith("AAAAA")
    assert text.endswith("BBBBB")
    assert PRUNE_MARKER in text


async def test_pruning_spans_several_text_blocks():
    root = await pruner(threshold_chars=60, head_chars=4, tail_chars=4)
    pruned = root.tool_result_pruner.prune_content(
        [TextBlock("A" * 40), TextBlock("B" * 40), TextBlock("C" * 40)]
    )
    text = "".join(b.text for b in pruned)
    assert text.startswith("AAAA")
    assert text.endswith("CCCC")
    assert text.count(PRUNE_MARKER) == 1  # exactly one marker, not one per block


async def test_non_text_blocks_keep_their_place():
    """R6.5 — the order a model reads is part of the meaning."""
    root = await pruner(threshold_chars=60, head_chars=4, tail_chars=4)
    call = ToolCallBlock(id="c1", name="x", arguments="{}")
    pruned = root.tool_result_pruner.prune_content(
        [TextBlock("A" * 60), call, TextBlock("B" * 60)]
    )
    assert call in pruned
    assert pruned.index(call) == 1


async def test_a_block_entirely_inside_the_removed_middle_collapses():
    root = await pruner(threshold_chars=60, head_chars=4, tail_chars=4)
    pruned = root.tool_result_pruner.prune_content(
        [TextBlock("A" * 40), TextBlock("M" * 60), TextBlock("B" * 40)]
    )
    text = "".join(b.text for b in pruned if isinstance(b, TextBlock))
    assert "M" not in text


async def test_pruning_is_deterministic():
    """R6.7 — a replay that produced different history would not be a replay."""
    root = await pruner(threshold_chars=60, head_chars=6, tail_chars=6)
    blocks = [TextBlock("A" * 60), ReasoningBlock("thinking"), TextBlock("B" * 60)]

    first = root.tool_result_pruner.prune_content(list(blocks))
    second = root.tool_result_pruner.prune_content(list(blocks))
    assert [b.text for b in first if isinstance(b, TextBlock)] == [
        b.text for b in second if isinstance(b, TextBlock)
    ]


async def test_a_prune_result_is_smaller_and_within_the_threshold():
    """Property 3 (I4)."""
    root = await pruner(threshold_chars=60, head_chars=5, tail_chars=5)
    original = [TextBlock("X" * 500)]
    before = root.tool_result_pruner.measure_content(original)

    pruned = root.tool_result_pruner.prune_content(original)
    after = root.tool_result_pruner.measure_content(pruned)

    assert after < before
    assert after <= 60


async def test_zero_head_and_tail_keeps_only_the_marker():
    root = await pruner(threshold_chars=60, head_chars=0, tail_chars=0)
    pruned = root.tool_result_pruner.prune_content([TextBlock("X" * 200)])
    assert "".join(b.text for b in pruned) == PRUNE_MARKER
