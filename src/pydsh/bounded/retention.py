"""Bounding a stream as it arrives, and saying honestly what was lost.

Two retainers, deliberately narrow. They count and they keep — they do not
sort, group, format, or interpret. Sorting a truncated set is the tool's job,
because only the tool knows whether the first N or the *best* N is what the
budget should have kept.

:class:`TextRetainer` is budgeted in **bytes**, not characters, because the
budget exists to bound memory and payload size and both are measured in bytes.
That brings the one genuinely fiddly problem here: a byte cut can land in the
middle of a UTF-8 character, and decoding then produces a replacement character
that was never in the data. So every cut is trimmed back to a boundary, and the
bytes lost to that trimming are counted as omitted too — otherwise the reported
number would be smaller than the real loss.
"""

from __future__ import annotations

from typing import Any, Union

from .omitted import assert_budget, omitted_exact, omitted_none

#: A UTF-8 continuation byte: `10xxxxxx`.
_CONTINUATION_MASK = 0xC0
_CONTINUATION_VALUE = 0x80

#: How many bytes a leading byte announces, by its high bits.
_LEAD_LENGTHS = ((0xF8, 0xF0, 4), (0xF0, 0xE0, 3), (0xE0, 0xC0, 2))

#: The furthest back a trailing partial character can begin.
_MAX_UTF8_WIDTH = 4


def _is_continuation(byte: int) -> bool:
    return byte & _CONTINUATION_MASK == _CONTINUATION_VALUE


def _lead_width(byte: int) -> int:
    """How many bytes this leading byte claims, or 1 for ASCII."""
    for mask, value, width in _LEAD_LENGTHS:
        if byte & mask == value:
            return width
    return 1


def trim_trailing_partial_utf8(data: bytes) -> bytes:
    """Drop a trailing character that is only half present.

    Walks back over continuation bytes to the leading byte, and cuts if that
    byte claims more bytes than are actually there.
    """
    if not data:
        return data
    index = len(data) - 1
    steps = 0
    while index >= 0 and _is_continuation(data[index]) and steps < _MAX_UTF8_WIDTH:
        index -= 1
        steps += 1
    if index < 0:
        return b""  # nothing but continuation bytes: no whole character here
    width = _lead_width(data[index])
    return data[:index] if index + width > len(data) else data


def trim_leading_continuation_utf8(data: bytes) -> bytes:
    """Drop leading bytes that are the tail of a character cut off before us."""
    index = 0
    while index < len(data) and _is_continuation(data[index]):
        index += 1
    return data[index:]


class ItemRetainer:
    """Keep the first N of a stream of items; count the rest exactly."""

    def __init__(self, max_items: int) -> None:
        assert_budget(max_items, "max_items")
        self._max_items = max_items
        self._items: list = []
        self._seen = 0
        self._omitted = 0

    def push(self, item: Any) -> dict:
        """Offer an item. Reports whether it was kept, and whether any was not."""
        self._seen += 1
        if len(self._items) < self._max_items:
            self._items.append(item)
            return {"kept": True, "truncated": self._omitted > 0}
        self._omitted += 1
        return {"kept": False, "truncated": True}

    def finish(self) -> dict:
        """What was kept, what was seen, and exactly what was dropped."""
        return {
            "items": list(self._items),
            "truncated": self._omitted > 0,
            "seen": self._seen,
            "kept": len(self._items),
            "omitted": omitted_exact(self._omitted) if self._omitted else omitted_none(),
        }


class TextRetainer:
    """Keep the first and/or last N *bytes* of a text stream.

    Build one with :meth:`head`, :meth:`tail`, or :meth:`head_tail` rather than
    the constructor — the strategy is what the caller is actually choosing.
    """

    def __init__(self, head_bytes: int = 0, tail_bytes: int = 0) -> None:
        assert_budget(head_bytes, "head_bytes")
        assert_budget(tail_bytes, "tail_bytes")
        self._head_cap = head_bytes
        self._tail_cap = tail_bytes
        self._head_chunks: list[bytes] = []
        self._head_held = 0
        self._tail_chunks: list[bytes] = []
        self._tail_held = 0
        self._total = 0

    @classmethod
    def head(cls, max_bytes: int) -> "TextRetainer":
        """Keep the beginning."""
        return cls(head_bytes=max_bytes)

    @classmethod
    def tail(cls, max_bytes: int) -> "TextRetainer":
        """Keep the end."""
        return cls(tail_bytes=max_bytes)

    @classmethod
    def head_tail(cls, head_bytes: int, tail_bytes: int) -> "TextRetainer":
        """Keep both ends and drop the middle."""
        return cls(head_bytes=head_bytes, tail_bytes=tail_bytes)

    def push(self, chunk: Union[bytes, str]) -> dict:
        """Offer a chunk. Never holds more than the configured budget."""
        data = chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
        before = self._total
        self._total += len(data)

        room = self._head_cap - self._head_held
        take = max(0, min(room, len(data)))
        if take:
            self._head_chunks.append(data[:take])
            self._head_held += take

        if self._tail_cap:
            self._tail_chunks.append(data)
            self._tail_held += len(data)
            while (
                self._tail_chunks
                and self._tail_held - len(self._tail_chunks[0]) >= self._tail_cap
            ):
                self._tail_held -= len(self._tail_chunks.pop(0))
            # The remaining leading chunk can still hold bytes that have slid
            # out of the window — a single chunk larger than the window is kept
            # whole by the loop above. Trimming it here is what makes memory
            # bounded by the *budget* rather than by the input, which is the
            # entire reason this class exists.
            if self._tail_chunks and self._tail_held > self._tail_cap:
                excess = self._tail_held - self._tail_cap
                self._tail_chunks[0] = self._tail_chunks[0][excess:]
                self._tail_held -= excess

        dropped_now = self._omitted_at(self._total) > self._omitted_at(before)
        return {"kept": not dropped_now, "truncated": self._omitted_at(self._total) > 0}

    def _omitted_at(self, total: int) -> int:
        """Bytes the budget would have dropped by the time ``total`` were seen."""
        head_len = min(total, self._head_cap)
        tail_len = min(total - head_len, self._tail_cap)
        return total - head_len - tail_len

    def finish(self) -> dict:
        """Decode what was kept, and report how many bytes were lost."""
        head_len = min(self._total, self._head_cap)
        tail_len = min(self._total - head_len, self._tail_cap)

        head = b"".join(self._head_chunks)
        tail = b"".join(self._tail_chunks)
        tail = tail[self._tail_held - tail_len :] if tail_len else b""

        if self._omitted_at(self._total) == 0:
            # Nothing was dropped, so head and tail are adjacent slices of one
            # stream. The split between them is an artefact of the strategy and
            # a character may span it — decoding separately would break that
            # character for no reason at all.
            return {
                "text": (head + tail).decode("utf-8", "replace"),
                "truncated": False,
                "omitted_bytes": omitted_none(),
            }

        kept_head = trim_trailing_partial_utf8(head)
        kept_tail = trim_leading_continuation_utf8(tail)
        # Counted against what is really returned: the boundary trim drops
        # bytes too, and reporting only the budget's share would understate it.
        omitted = self._total - len(kept_head) - len(kept_tail)
        return {
            "text": kept_head.decode("utf-8", "replace")
            + kept_tail.decode("utf-8", "replace"),
            "truncated": omitted > 0,
            "omitted_bytes": omitted_exact(omitted) if omitted else omitted_none(),
        }


__all__ = [
    "ItemRetainer",
    "TextRetainer",
    "trim_trailing_partial_utf8",
    "trim_leading_continuation_utf8",
]
