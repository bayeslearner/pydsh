"""``ctx.fs`` — the file system as a capability, with a containment story.

This is the one seam where the *argument* is the thing that escapes. A command
string is dangerous because of what it says; a path is dangerous because of
where it points, and where it points is not always where it looks.

Hence the two rules this module exists to get right:

**Containment resolves symlinks.** Normalising ``..`` away is not containment.
A symlink inside the root pointing at ``/etc`` survives a lexical check and
then reads ``/etc`` anyway. The check compares fully resolved paths on both
sides, which is the only version that holds.

**A limit bounds the cost, not the description.** Reading a whole file and then
returning the first megabyte protects the model's context and not the process.
Reads here stream, and stop when the budget is spent.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator, Optional

from plugkit import Service

from ..storage import write_file_atomic

#: Lines a read returns unless asked otherwise.
READ_LIMIT = 2000

#: Longest single line returned before it is truncated.
READ_MAX_LINE_LENGTH = 2000

#: Bytes one read may return. Spent as the file is read, not applied after.
READ_MAX_BYTES = 1024 * 1024

#: Broadcast before a write lands, so a guard can observe it.
WRITE_INTENT = "fs/write-intent"

#: Marks a line the read had to cut short.
TRUNCATION_MARK = "…"


class PathOutsideRootError(PermissionError):
    """A path resolved outside the configured execution root."""


class AmbiguousEditError(ValueError):
    """An edit matched more than once, so which one was meant is unknown."""


class FileSystem(Service):
    """Provides ``ctx.fs``."""

    provide = "fs"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        root = config.get("root")
        # Resolved once, and resolved *fully*: a root reached through a symlink
        # must still match its own contents when they are resolved too.
        self._root = Path(root).resolve() if root else None

    @property
    def root(self) -> Optional[Path]:
        """The execution root, or ``None`` when unrestricted."""
        return self._root

    # -- paths ------------------------------------------------------------- #
    def resolve(self, path: str, cwd: Optional[str] = None) -> str:
        """An absolute path, checked against the execution root.

        :raises PathOutsideRootError: the path resolves outside the root —
            including *through* a symlink, which is the case a lexical check
            misses.
        """
        if not path:
            raise ValueError("a path is required")
        absolute = Path(cwd or os.getcwd()) / path
        if self._root is None:
            return str(absolute.resolve())

        # `resolve()` on a path that does not exist yet resolves the part that
        # does, which is what makes writing a new file inside the root work.
        real = absolute.resolve()
        if real != self._root and self._root not in real.parents:
            raise PathOutsideRootError(
                f"{str(real)!r} is outside the execution root {str(self._root)!r}"
            )
        return str(real)

    # -- reading ----------------------------------------------------------- #
    def read_text(
        self,
        path: str,
        offset: int = 1,
        limit: int = READ_LIMIT,
        max_line_length: int = READ_MAX_LINE_LENGTH,
        max_bytes: int = READ_MAX_BYTES,
        cwd: Optional[str] = None,
    ) -> dict:
        """A numbered window of a text file, bounded in lines and in bytes.

        Streams: the file is read line by line and the read stops once the byte
        budget is spent, so the limit bounds what this *costs* rather than only
        what it returns.
        """
        absolute = Path(self.resolve(path, cwd))
        if absolute.is_dir():
            raise IsADirectoryError(f"{absolute} is a directory, not a text file")
        if not absolute.exists():
            raise FileNotFoundError(f"no such file: {absolute}")

        lines: list[tuple[int, str]] = []
        budget = max_bytes
        truncated = False
        total = 0
        collecting = True

        with absolute.open("r", encoding="utf-8", errors="replace") as handle:
            for number, raw in enumerate(handle, start=1):
                total = number
                if not collecting or number < offset or len(lines) >= limit:
                    # Still counting, so `total_lines` is honest, but no longer
                    # holding anything: the window is full or the budget is out.
                    continue
                text = raw.rstrip("\n")
                if len(text) > max_line_length:
                    text = text[:max_line_length] + TRUNCATION_MARK
                    truncated = True
                cost = len(text.encode("utf-8"))
                if cost > budget:
                    truncated = True
                    collecting = False
                    continue
                budget -= cost
                lines.append((number, text))

        if len(lines) >= limit and total > offset - 1 + limit:
            truncated = True
        return {
            "path": str(absolute),
            "total_lines": total,
            "lines": lines,
            "truncated": truncated,
        }

    # -- writing ----------------------------------------------------------- #
    def write_text(
        self, path: str, content: str, cwd: Optional[str] = None
    ) -> dict:
        """Replace a file's contents atomically."""
        absolute = self.resolve(path, cwd)
        # Announced before the write, so a guard sees the intent rather than
        # the aftermath.
        self.ctx.emit(WRITE_INTENT, {"path": absolute})
        write_file_atomic(absolute, content)
        return {"path": absolute, "bytes": len(content.encode("utf-8"))}

    def edit_text(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        cwd: Optional[str] = None,
    ) -> dict:
        """Replace an exact string, refusing anything ambiguous.

        :raises AmbiguousEditError: ``old_string`` occurs more than once and the
            caller did not ask for all of them. Picking the first would edit a
            line the caller never looked at, and nothing would report it.
        """
        if old_string == new_string:
            raise ValueError("edit_text was given identical old and new text")
        if not old_string:
            raise ValueError("edit_text needs a non-empty string to replace")

        absolute = Path(self.resolve(path, cwd))
        content = absolute.read_text(encoding="utf-8")
        occurrences = content.count(old_string)
        if occurrences == 0:
            raise ValueError(f"{absolute}: the text to replace was not found")
        if occurrences > 1 and not replace_all:
            raise AmbiguousEditError(
                f"{absolute}: the text to replace appears {occurrences} times; "
                "pass replace_all=True or give more surrounding context"
            )

        updated = content.replace(
            old_string, new_string, -1 if replace_all else 1
        )
        self.ctx.emit(WRITE_INTENT, {"path": str(absolute)})
        write_file_atomic(absolute, updated)
        return {
            "path": str(absolute),
            "replacements": occurrences if replace_all else 1,
            "bytes": len(updated.encode("utf-8")),
        }

    # -- inspecting -------------------------------------------------------- #
    def list(self, path: str = ".", cwd: Optional[str] = None) -> list[dict]:
        """A directory's entries, sorted, each with its kind and size."""
        absolute = Path(self.resolve(path, cwd))
        if not absolute.is_dir():
            raise NotADirectoryError(f"{absolute} is not a directory")
        entries = []
        for child in sorted(absolute.iterdir(), key=lambda p: p.name):
            try:
                size = child.stat().st_size
            except OSError:
                size = 0  # a broken symlink still deserves a listing
            entries.append(
                {"name": child.name, "path": str(child), "is_dir": child.is_dir(), "size": size}
            )
        return entries

    def exists(self, path: str, cwd: Optional[str] = None) -> bool:
        return Path(self.resolve(path, cwd)).exists()

    def info(self, path: str, cwd: Optional[str] = None) -> dict:
        """What is at a path — without raising when the answer is "nothing"."""
        absolute = Path(self.resolve(path, cwd))
        if not absolute.exists():
            return {"path": str(absolute), "exists": False}
        stat = absolute.stat()
        return {
            "path": str(absolute),
            "exists": True,
            "is_dir": absolute.is_dir(),
            "size": stat.st_size,
            "modified_at": stat.st_mtime,
        }


__all__ = [
    "FileSystem",
    "PathOutsideRootError",
    "AmbiguousEditError",
    "READ_LIMIT",
    "READ_MAX_LINE_LENGTH",
    "READ_MAX_BYTES",
    "WRITE_INTENT",
]
