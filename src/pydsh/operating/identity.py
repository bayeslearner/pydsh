"""A stable identifier for this machine, for telemetry that has no user.

Written once under the home directory and read thereafter. Not a user account
and not an identity claim — there is no auth anywhere in this layer, by design.
It exists so that "how many distinct installs hit this problem" is answerable
without anything identifying being collected.

Degrades rather than fails. A read-only or missing home directory gives a
per-process identifier instead: telemetry loses continuity, which is a much
smaller problem than a harness that will not start.
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any, Optional

from plugkit import Service

from ..storage import write_file_atomic

logger = logging.getLogger("pydsh.identity")

#: Where the id lives, under the home directory.
ID_FILE_NAME = ".anonymous-user-id"

_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


def _read_stored(path: Path) -> Optional[str]:
    """The stored id, if there is a valid one.

    Anything that is not a UUID is treated as absent and replaced. A truncated
    or hand-edited file should produce a fresh id, not a value that propagates
    into every later record.
    """
    try:
        stored = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return stored if _UUID.fullmatch(stored) else None


def get_or_create_anonymous_user_id(home: Optional[Path] = None) -> str:
    """This machine's id, creating and storing one on first use."""
    root = home or Path.home()
    path = root / ID_FILE_NAME

    stored = _read_stored(path)
    if stored is not None:
        return stored

    fresh = str(uuid.uuid4())
    try:
        write_file_atomic(path, fresh)
    except OSError as exc:
        # Read-only home, no home, a locked-down container: the id is still
        # usable, it just will not survive this process.
        logger.info(
            "anonymous id could not be stored at %s (%s); using a per-process id",
            path,
            exc,
        )
    return fresh


class AnonymousUserId(Service):
    """Provides ``ctx.anonymous_user_id``."""

    provide = "anonymous_user_id"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        home = config.get("home")
        self.value = get_or_create_anonymous_user_id(Path(home) if home else None)

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.value


__all__ = ["AnonymousUserId", "get_or_create_anonymous_user_id", "ID_FILE_NAME"]
