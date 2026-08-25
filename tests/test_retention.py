"""Retention — Requirements 1 to 4, properties 1 and 2.

The two properties are the ones that make retention worth having: memory
bounded by the budget rather than the input, and a cut that never invents a
broken character.
"""

from __future__ import annotations

import pytest

from pydsh.bounded import (
    ItemRetainer,
    TextRetainer,
    describe_omitted,
    format_retention_notice,
    omitted_exact,
    omitted_none,
    omitted_unknown,
)

# A three-byte character, so a byte cut can land inside it.
SNOW = "☃"
assert len(SNOW.encode()) == 3


# --------------------------------------------------------------------------- #
# The omitted vocabulary (R1)
# --------------------------------------------------------------------------- #
def test_nothing_omitted_reads_as_nothing():
    """So a caller can join it unconditionally."""
    assert describe_omitted(omitted_none(), "matches") == ""


def test_an_exact_count_is_stated():
    assert describe_omitted(omitted_exact(4812), "matches") == "Omitted 4812 matches."


def test_an_unknown_amount_does_not_invent_a_number():
    """R1.3 — the third fact a boolean would have collapsed."""
    assert describe_omitted(omitted_unknown(), "matches") == "More matches were omitted."


def test_a_notice_joins_the_loss_and_the_recovery():
    notice = {"omitted": omitted_exact(12), "unit": "files", "kept": 5}
    line = format_retention_notice(notice, lambda n: f"Kept {n['kept']}; narrow the glob.")
    assert line == "Omitted 12 files. Kept 5; narrow the glob."


def test_a_notice_with_nothing_omitted_is_just_the_recovery():
    notice = {"omitted": omitted_none(), "unit": "files"}
    assert format_retention_notice(notice, lambda n: "All results shown.") == "All results shown."


def test_a_notice_with_no_recovery_is_just_the_loss():
    notice = {"omitted": omitted_exact(3), "unit": "files"}
    assert format_retention_notice(notice, lambda n: "") == "Omitted 3 files."


def test_an_empty_notice_is_empty_rather_than_a_stray_space():
    notice = {"omitted": omitted_none(), "unit": "files"}
    assert format_retention_notice(notice, lambda n: "") == ""


# --------------------------------------------------------------------------- #
# ItemRetainer (R2)
# --------------------------------------------------------------------------- #
def test_items_within_the_budget_are_all_kept():
    retainer = ItemRetainer(5)
    for i in range(3):
        assert retainer.push(i) == {"kept": True, "truncated": False}
    result = retainer.finish()
    assert result["items"] == [0, 1, 2]
    assert result["truncated"] is False
    assert result["omitted"] == omitted_none()


def test_items_beyond_the_budget_are_counted_exactly():
    retainer = ItemRetainer(2)
    for i in range(10):
        retainer.push(i)
    result = retainer.finish()
    assert result["items"] == [0, 1]
    assert result["seen"] == 10
    assert result["kept"] == 2
    assert result["omitted"] == omitted_exact(8)


def test_push_reports_the_moment_it_starts_dropping():
    retainer = ItemRetainer(1)
    assert retainer.push("a") == {"kept": True, "truncated": False}
    assert retainer.push("b") == {"kept": False, "truncated": True}


def test_a_zero_budget_keeps_nothing_but_still_counts():
    retainer = ItemRetainer(0)
    for i in range(4):
        retainer.push(i)
    result = retainer.finish()
    assert result["items"] == []
    assert result["omitted"] == omitted_exact(4)


def test_a_negative_budget_is_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        ItemRetainer(-1)


def test_a_non_integer_budget_is_rejected():
    with pytest.raises(ValueError):
        ItemRetainer(2.5)


# --------------------------------------------------------------------------- #
# TextRetainer (R3) — properties 1 and 2
# --------------------------------------------------------------------------- #
def test_head_keeps_the_beginning():
    retainer = TextRetainer.head(5)
    retainer.push("abcdefghij")
    result = retainer.finish()
    assert result["text"] == "abcde"
    assert result["omitted_bytes"] == omitted_exact(5)


def test_tail_keeps_the_end():
    retainer = TextRetainer.tail(5)
    retainer.push("abcdefghij")
    assert retainer.finish()["text"] == "fghij"


def test_head_tail_keeps_both_ends():
    retainer = TextRetainer.head_tail(3, 3)
    retainer.push("abcdefghij")
    result = retainer.finish()
    assert result["text"] == "abchij"
    assert result["omitted_bytes"] == omitted_exact(4)


def test_text_within_the_budget_is_untouched():
    retainer = TextRetainer.head_tail(50, 50)
    retainer.push("short")
    result = retainer.finish()
    assert result["text"] == "short"
    assert result["truncated"] is False


def test_a_stream_of_chunks_is_reassembled():
    retainer = TextRetainer.head(6)
    for chunk in ("ab", "cd", "ef", "gh"):
        retainer.push(chunk)
    assert retainer.finish()["text"] == "abcdef"


def test_memory_is_bounded_by_the_budget_not_the_input():
    """Property 1 (I3) — the reason a single huge chunk needs trimming too."""
    retainer = TextRetainer.head_tail(10, 10)
    for _ in range(100):
        retainer.push("x" * 10_000)  # a megabyte, in chunks bigger than the window

    held = sum(len(c) for c in retainer._head_chunks) + sum(
        len(c) for c in retainer._tail_chunks
    )
    assert held <= 20
    assert len(retainer.finish()["text"]) == 20


def test_one_chunk_larger_than_the_tail_window_is_trimmed():
    retainer = TextRetainer.tail(4)
    retainer.push("abcdefghij")
    assert sum(len(c) for c in retainer._tail_chunks) == 4
    assert retainer.finish()["text"] == "ghij"


# --------------------------------------------------------------------------- #
# Property 2 — UTF-8 boundaries
# --------------------------------------------------------------------------- #
def test_a_cut_through_a_character_does_not_produce_a_replacement():
    """Property 2 (I2) — the whole reason boundaries are trimmed.

    Four snowmen are twelve bytes. A head budget of 5 lands inside the second
    one; decoding that naively yields '☃��'.
    """
    retainer = TextRetainer.head(5)
    retainer.push(SNOW * 4)
    result = retainer.finish()

    assert "�" not in result["text"]
    assert result["text"] == SNOW


def test_a_tail_cut_through_a_character_does_not_produce_a_replacement():
    retainer = TextRetainer.tail(5)
    retainer.push(SNOW * 4)
    result = retainer.finish()
    assert "�" not in result["text"]
    assert result["text"] == SNOW


def test_the_omitted_count_includes_the_boundary_trim():
    """R3.6 — reporting only the budget's share would understate the loss."""
    retainer = TextRetainer.head(5)
    retainer.push(SNOW * 4)  # 12 bytes; 5 budgeted, 3 actually kept
    assert retainer.finish()["omitted_bytes"] == omitted_exact(9)


def test_a_character_spanning_the_head_tail_split_survives_when_nothing_is_lost():
    """R3.5 — the split is an artefact; a character may sit across it.

    Six bytes, budgeted three and three: the middle character straddles the
    boundary. Decoding the halves separately would break it for no reason.
    """
    text = "a" + SNOW + "bc"  # 1 + 3 + 2 = 6 bytes
    retainer = TextRetainer.head_tail(3, 3)
    retainer.push(text)
    result = retainer.finish()

    assert result["text"] == text
    assert result["truncated"] is False
    assert "�" not in result["text"]


def test_a_character_split_across_two_pushes_survives():
    retainer = TextRetainer.head(100)
    encoded = SNOW.encode()
    retainer.push(encoded[:1])
    retainer.push(encoded[1:])
    assert retainer.finish()["text"] == SNOW


def test_bytes_and_text_can_be_mixed():
    retainer = TextRetainer.head(100)
    retainer.push("ab")
    retainer.push(b"cd")
    assert retainer.finish()["text"] == "abcd"


def test_a_negative_text_budget_is_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        TextRetainer.head(-1)
