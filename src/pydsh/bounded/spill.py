"""Putting output somewhere the model can go back to.

Truncating a tool result protects the context and loses the data. Spilling
keeps both: the whole output goes to a session-scoped file, and the tool returns
a locator the model can read, grep, or window into — a path instead of a wall.

Unlike almost everything else in this port, **a spill is not derived**. It is
the only copy of something too big to keep, which makes its retention the
consumer's problem rather than something this layer can quietly clean up. The
data-architecture row says so explicitly.

Path segments are encoded rather than trusted. A session id and a suggested
name both reach here from elsewhere, and either one containing a separator or a
``..`` would put the file outside the root that is supposed to contain it.
"""

from __future__ import annotations

import os
import re
from abc import abstractmethod
from pathlib import Path
from typing import Any, Optional

from plugkit import Service

from ..storage import write_file_atomic

#: Characters allowed through unencoded in a path segment.
_SAFE = re.compile(r"[A-Za-z0-9._-]")

#: Where spills go when a deployment does not say. Under the user's home so it
#: inherits home's permissions rather than a world-readable temp directory.
DEFAULT_ROOT_NAME = ".pydsh-spill"

#: What a tool tells the model to do with a locator.
RETRIEVAL_HINT = (
    "Read this path with an offset and limit, or grep it, to search within it."
)


def encode_segment(raw: str) -> str:
    """Make one path segment safe.

    Anything outside the safe set becomes a percent-escape, so a separator, a
    ``..``, or a null byte cannot survive into a path. A leading dot is escaped
    too — otherwise ``..`` encodes to itself.
    """
    if not raw:
        return "_"
    out = []
    for index, char in enumerate(raw):
        if _SAFE.fullmatch(char) and not (index == 0 and char == "."):
            out.append(char)
        else:
            out.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
    return "".join(out)


def private_root() -> Path:
    """The default spill root, private to this user."""
    return Path.home() / DEFAULT_ROOT_NAME


class SpillStore(Service):
    """``ctx.spill`` — where oversized output goes. An interface.

    A consumer that needs spills somewhere else — object storage, an encrypted
    volume, nowhere at all — implements this and mounts that instead.
    """

    provide = "spill"

    @abstractmethod
    async def save_text(
        self, session_id: str, suggested_name: str, content: str
    ) -> dict:
        """Persist ``content`` as a session-scoped artifact.

        :returns: ``{"locator", "bytes", "retrieval_hint"}``.
        :raises: on a real storage failure. Never returns quietly — a caller
            that believes it saved the output will hand the model a locator
            pointing at nothing.
        """
        raise NotImplementedError


class LocalSpillStore(SpillStore):
    """Spills to the local filesystem, one directory per session."""

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        root = config.get("root")
        self.root = Path(root).resolve() if root else private_root()

    def session_dir(self, session_id: str) -> Path:
        """This session's directory, with the id encoded so it cannot escape."""
        return self.root / encode_segment(session_id)

    async def save_text(
        self, session_id: str, suggested_name: str, content: str
    ) -> dict:
        directory = self.session_dir(session_id)
        target = directory / encode_segment(suggested_name)

        # Belt and braces: the encoding above already makes traversal
        # impossible, and this catches a future change to it.
        resolved = (directory / target.name).resolve()
        if self.root not in resolved.parents:
            raise ValueError(
                f"spill target {str(resolved)!r} is outside the spill root "
                f"{str(self.root)!r}"
            )

        encoded = content.encode("utf-8")
        write_file_atomic(resolved, content)
        try:
            # The root holds whatever a command printed, which may include
            # secrets. Owner-only, so it is not readable by other accounts.
            os.chmod(self.root, 0o700)
        except OSError:
            pass  # a filesystem without modes; the write itself still stands

        return {
            "locator": str(resolved),
            "bytes": len(encoded),
            "retrieval_hint": RETRIEVAL_HINT,
        }


__all__ = [
    "SpillStore",
    "LocalSpillStore",
    "encode_segment",
    "private_root",
    "RETRIEVAL_HINT",
    "DEFAULT_ROOT_NAME",
]
