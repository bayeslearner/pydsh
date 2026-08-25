"""The file system seam — Requirements 1 to 3, properties 1 and 2.

Real files in a temp directory, including a real symlink: the containment
defect this fixes is invisible to a test that only tries `..`.
"""

from __future__ import annotations

import os

import pytest

from plugkit import Context

from pydsh.capability import (
    WRITE_INTENT,
    AmbiguousEditError,
    FileSystem,
    PathOutsideRootError,
)

pytestmark = pytest.mark.asyncio


async def mounted(root=None) -> Context:
    ctx = Context()
    await ctx.plugin(FileSystem, {"root": str(root)} if root else {})
    return ctx


# --------------------------------------------------------------------------- #
# Containment (R1) — property 1
# --------------------------------------------------------------------------- #
async def test_a_path_inside_the_root_resolves(tmp_path):
    root = await mounted(tmp_path)
    (tmp_path / "a.txt").write_text("hi")
    assert root.fs.resolve(str(tmp_path / "a.txt")) == str(tmp_path / "a.txt")


async def test_a_path_outside_the_root_is_rejected(tmp_path):
    root = await mounted(tmp_path / "inside")
    (tmp_path / "inside").mkdir()
    with pytest.raises(PathOutsideRootError):
        root.fs.resolve(str(tmp_path / "outside.txt"))


async def test_dot_dot_cannot_climb_out(tmp_path):
    inside = tmp_path / "inside"
    inside.mkdir()
    root = await mounted(inside)
    with pytest.raises(PathOutsideRootError):
        root.fs.resolve("../escaped.txt", cwd=str(inside))


async def test_a_symlink_out_of_the_root_is_rejected(tmp_path):
    """Property 1 (I1) — the defect a lexical check misses entirely.

    `os.path.abspath` normalises `..` and does *not* follow links, so a link
    inside the root pointing at /etc passes the check and then reads /etc.
    """
    inside = tmp_path / "inside"
    inside.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("not yours")
    os.symlink(secret, inside / "innocent.txt")

    root = await mounted(inside)
    with pytest.raises(PathOutsideRootError):
        root.fs.resolve(str(inside / "innocent.txt"))


async def test_a_symlink_to_a_directory_out_of_the_root_is_rejected(tmp_path):
    inside = tmp_path / "inside"
    inside.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "f.txt").write_text("x")
    os.symlink(tmp_path / "elsewhere", inside / "link")

    root = await mounted(inside)
    with pytest.raises(PathOutsideRootError):
        root.fs.resolve(str(inside / "link" / "f.txt"))


async def test_a_symlink_within_the_root_is_allowed(tmp_path):
    """Containment, not a ban on links."""
    inside = tmp_path / "inside"
    inside.mkdir()
    (inside / "real.txt").write_text("mine")
    os.symlink(inside / "real.txt", inside / "alias.txt")

    root = await mounted(inside)
    assert root.fs.read_text(str(inside / "alias.txt"))["lines"] == [(1, "mine")]


async def test_a_root_reached_through_a_symlink_still_matches_itself(tmp_path):
    """R1.4 — both sides are resolved, or a linked root excludes its own files."""
    real = tmp_path / "real-root"
    real.mkdir()
    (real / "f.txt").write_text("ok")
    linked = tmp_path / "linked-root"
    os.symlink(real, linked)

    root = await mounted(linked)
    assert root.fs.read_text(str(linked / "f.txt"))["lines"] == [(1, "ok")]


async def test_a_new_file_inside_the_root_is_allowed(tmp_path):
    """Resolution of a path that does not exist yet resolves its prefix."""
    root = await mounted(tmp_path)
    result = root.fs.write_text(str(tmp_path / "new" / "file.txt"), "made")
    assert (tmp_path / "new" / "file.txt").read_text() == "made"
    assert result["bytes"] == 4


async def test_no_root_means_no_restriction(tmp_path):
    root = await mounted()
    (tmp_path / "anywhere.txt").write_text("fine")
    assert root.fs.exists(str(tmp_path / "anywhere.txt"))


async def test_an_empty_path_is_rejected():
    root = await mounted()
    with pytest.raises(ValueError, match="path is required"):
        root.fs.resolve("")


# --------------------------------------------------------------------------- #
# Reading (R2) — property 2
# --------------------------------------------------------------------------- #
async def test_a_read_returns_numbered_lines(tmp_path):
    root = await mounted()
    target = tmp_path / "f.txt"
    target.write_text("one\ntwo\nthree\n")
    result = root.fs.read_text(str(target))
    assert result["lines"] == [(1, "one"), (2, "two"), (3, "three")]
    assert result["total_lines"] == 3
    assert result["truncated"] is False


async def test_a_window_starts_at_the_offset(tmp_path):
    root = await mounted()
    target = tmp_path / "f.txt"
    target.write_text("\n".join(f"line {i}" for i in range(1, 11)))
    result = root.fs.read_text(str(target), offset=4, limit=2)
    assert result["lines"] == [(4, "line 4"), (5, "line 5")]
    assert result["total_lines"] == 10
    assert result["truncated"] is True


async def test_a_file_without_a_trailing_newline_keeps_its_last_line(tmp_path):
    root = await mounted()
    target = tmp_path / "f.txt"
    target.write_text("only")
    assert root.fs.read_text(str(target))["lines"] == [(1, "only")]


async def test_an_over_long_line_is_truncated_and_marked(tmp_path):
    root = await mounted()
    target = tmp_path / "f.txt"
    target.write_text("x" * 50)
    result = root.fs.read_text(str(target), max_line_length=10)
    assert result["lines"][0][1] == "x" * 10 + "…"
    assert result["truncated"] is True


async def test_the_byte_budget_stops_the_read(tmp_path):
    """Property 2 (I2) — the limit bounds the cost, not just the answer."""
    root = await mounted()
    target = tmp_path / "big.txt"
    target.write_text("\n".join("y" * 100 for _ in range(1000)))

    result = root.fs.read_text(str(target), max_bytes=250)
    assert result["truncated"] is True
    assert len(result["lines"]) == 2  # 100 bytes each; the third would not fit
    # The whole file was still counted, so the caller knows what it is missing.
    assert result["total_lines"] == 1000


async def test_reading_a_directory_says_so(tmp_path):
    root = await mounted()
    with pytest.raises(IsADirectoryError):
        root.fs.read_text(str(tmp_path))


async def test_reading_a_missing_file_says_so(tmp_path):
    root = await mounted()
    with pytest.raises(FileNotFoundError):
        root.fs.read_text(str(tmp_path / "nope.txt"))


async def test_invalid_utf8_is_replaced_not_fatal(tmp_path):
    """R2.5 — one bad byte must not make a file unreadable."""
    root = await mounted()
    target = tmp_path / "f.bin"
    target.write_bytes(b"good\xff\xfebad\n")
    assert "good" in root.fs.read_text(str(target))["lines"][0][1]


# --------------------------------------------------------------------------- #
# Writing and editing (R3)
# --------------------------------------------------------------------------- #
async def test_a_write_replaces_and_reports_bytes(tmp_path):
    root = await mounted()
    target = tmp_path / "f.txt"
    root.fs.write_text(str(target), "first")
    result = root.fs.write_text(str(target), "second")
    assert target.read_text() == "second"
    assert result["bytes"] == 6


async def test_a_write_announces_its_intent_first(tmp_path):
    """R3.2 — a guard should see the intent, not the aftermath."""
    root = await mounted()
    seen = []
    root.on(WRITE_INTENT, lambda payload: seen.append(payload["path"]))
    target = tmp_path / "f.txt"
    root.fs.write_text(str(target), "x")
    assert seen == [str(target)]


async def test_an_edit_replaces_exactly(tmp_path):
    root = await mounted()
    target = tmp_path / "f.txt"
    target.write_text("hello world")
    root.fs.edit_text(str(target), "world", "there")
    assert target.read_text() == "hello there"


async def test_an_edit_that_matches_nothing_raises(tmp_path):
    root = await mounted()
    target = tmp_path / "f.txt"
    target.write_text("hello")
    with pytest.raises(ValueError, match="not found"):
        root.fs.edit_text(str(target), "absent", "x")


async def test_an_ambiguous_edit_is_refused(tmp_path):
    """R3.4 — picking the first match edits a line nobody looked at."""
    root = await mounted()
    target = tmp_path / "f.txt"
    target.write_text("a\na\na\n")
    with pytest.raises(AmbiguousEditError, match="appears 3 times"):
        root.fs.edit_text(str(target), "a", "b")
    assert target.read_text() == "a\na\na\n"  # untouched


async def test_replace_all_is_the_way_to_mean_it(tmp_path):
    root = await mounted()
    target = tmp_path / "f.txt"
    target.write_text("a\na\na\n")
    result = root.fs.edit_text(str(target), "a", "b", replace_all=True)
    assert target.read_text() == "b\nb\nb\n"
    assert result["replacements"] == 3


async def test_an_edit_to_identical_text_is_refused(tmp_path):
    root = await mounted()
    target = tmp_path / "f.txt"
    target.write_text("same")
    with pytest.raises(ValueError, match="identical"):
        root.fs.edit_text(str(target), "same", "same")


# --------------------------------------------------------------------------- #
# Inspecting (R3.5, R3.6)
# --------------------------------------------------------------------------- #
async def test_listing_reports_kind_and_size(tmp_path):
    root = await mounted()
    (tmp_path / "a.txt").write_text("12345")
    (tmp_path / "sub").mkdir()
    entries = root.fs.list(str(tmp_path))
    assert [(e["name"], e["is_dir"], e["size"]) for e in entries] == [
        ("a.txt", False, 5),
        ("sub", True, entries[1]["size"]),
    ]


async def test_listing_a_file_is_an_error(tmp_path):
    root = await mounted()
    (tmp_path / "a.txt").write_text("x")
    with pytest.raises(NotADirectoryError):
        root.fs.list(str(tmp_path / "a.txt"))


async def test_a_broken_symlink_still_appears_in_a_listing(tmp_path):
    root = await mounted()
    os.symlink(tmp_path / "gone", tmp_path / "dangling")
    assert [e["name"] for e in root.fs.list(str(tmp_path))] == ["dangling"]


async def test_info_answers_for_a_missing_path_without_raising(tmp_path):
    root = await mounted()
    assert root.fs.info(str(tmp_path / "nope")) == {
        "path": str(tmp_path / "nope"),
        "exists": False,
    }


async def test_info_describes_what_is_there(tmp_path):
    root = await mounted()
    (tmp_path / "a.txt").write_text("12345")
    info = root.fs.info(str(tmp_path / "a.txt"))
    assert info["exists"] is True
    assert info["is_dir"] is False
    assert info["size"] == 5
