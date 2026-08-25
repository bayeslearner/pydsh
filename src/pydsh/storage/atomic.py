"""Replacing a file's contents without ever leaving it half-written.

Write to a temporary file beside the target, flush it to the platter, then
rename over the target. ``os.replace`` is atomic within a filesystem, so a
reader sees either the whole old file or the whole new one — never the
truncated middle a plain ``open(path, "w")`` leaves behind if the process dies
between truncating and writing.

The temporary lives in the *same directory* as the target on purpose: a rename
across filesystems is not atomic, and the system temp directory is very often
a different filesystem.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Union


def write_file_atomic(
    filename: Union[str, os.PathLike], data: Union[str, bytes]
) -> None:
    """Replace ``filename``'s contents in one step, creating parents as needed.

    On any failure the original file is left exactly as it was, and the
    temporary is removed.
    """
    target = Path(filename)
    target.parent.mkdir(parents=True, exist_ok=True)

    binary = isinstance(data, bytes)
    handle = tempfile.NamedTemporaryFile(
        mode="wb" if binary else "w",
        encoding=None if binary else "utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(data)
            handle.flush()
            # Without this the rename can land before the bytes do, and a
            # crash leaves an intact-looking file full of zeros.
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


__all__ = ["write_file_atomic"]
