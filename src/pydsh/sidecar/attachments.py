"""``ctx.attachments`` — immutable binary content, named by what it is.

A reference is a **content address**, `sha256:<hex>`, and deliberately not a
path or a URL. A path handed to a model is also an instruction about where to
look; a bearer URL is a credential that leaks the moment it is logged. A
content address is neither — it names the bytes and nothing else.

It also makes the integrity check free. Reading re-hashes and compares, so a
file that was swapped or corrupted on disk is *caught* rather than served as
though it were the original.

Validation happens before storage, never after. A reference handed out before
the bytes were accepted points at something that was never checked, and the
caller has no way to tell.
"""

from __future__ import annotations

import hashlib
from abc import abstractmethod
from pathlib import Path
from typing import Any, Optional

from plugkit import Service

from ..storage import write_file_atomic

#: The largest image accepted. Generous enough for a screenshot, small enough
#: that a mistake does not fill a disk.
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024

#: What the store will accept. Validation checks the *declared* type against
#: this; decoding the format belongs to a consumer's library, not a
#: stdlib-only core.
DEFAULT_ALLOWED_IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")

#: How an id is spelled.
ID_PREFIX = "sha256:"


class AttachmentError(Exception):
    """A stable failure class. Callers route on ``code``, not on the type."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def content_id(data: bytes) -> str:
    """The address of some bytes. The same bytes always give the same id."""
    return f"{ID_PREFIX}{hashlib.sha256(data).hexdigest()}"


class AttachmentStore(Service):
    """The seam. A consumer storing attachments elsewhere implements this."""

    provide = "attachments"

    @abstractmethod
    def image_limits(self) -> dict: ...

    @abstractmethod
    def validate_image(self, data: bytes, declared_type: str) -> dict: ...

    @abstractmethod
    async def save_image(self, data: bytes, declared_type: str) -> dict: ...

    @abstractmethod
    async def read_image(self, attachment_id: str) -> bytes: ...


class LocalAttachments(AttachmentStore):
    """Content-addressed files under one root."""

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        root = config.get("root")
        self.root = (
            Path(root).resolve() if root else Path.home() / ".pydsh-attachments"
        )
        self._max_bytes = int(config.get("max_image_bytes", DEFAULT_MAX_IMAGE_BYTES))
        self._allowed = tuple(
            config.get("allowed_image_types", DEFAULT_ALLOWED_IMAGE_TYPES)
        )

    def image_limits(self) -> dict:
        return {"max_bytes": self._max_bytes, "allowed_types": list(self._allowed)}

    def validate_image(self, data: bytes, declared_type: str) -> dict:
        """Check the bytes. **Stores nothing** — that is the whole point.

        A caller can ask "would this be accepted" without committing to it, and
        `save_image` runs the same check before writing, so the two can never
        disagree.
        """
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise AttachmentError("attachment-empty", "an attachment needs content")
        if len(data) > self._max_bytes:
            raise AttachmentError(
                "attachment-too-large",
                f"attachment is {len(data)} bytes, over the limit of {self._max_bytes}",
            )
        if declared_type not in self._allowed:
            raise AttachmentError(
                "attachment-unsupported-type",
                f"type {declared_type!r} is not accepted; allowed: "
                f"{', '.join(self._allowed)}",
            )
        return {
            "id": content_id(bytes(data)),
            "bytes": len(data),
            "media_type": declared_type,
        }

    def _path_for(self, attachment_id: str) -> Path:
        if not attachment_id.startswith(ID_PREFIX):
            raise AttachmentError(
                "attachment-invalid-id", f"{attachment_id!r} is not an attachment id"
            )
        digest = attachment_id[len(ID_PREFIX):]
        if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
            raise AttachmentError(
                "attachment-invalid-id", f"{attachment_id!r} is not a sha256 address"
            )
        # Sharded so one directory does not accumulate every attachment. The
        # digest is hex by construction, so no segment can escape the root.
        return self.root / digest[:2] / digest

    async def save_image(self, data: bytes, declared_type: str) -> dict:
        """Validate, then store, then reference — in that order (I1)."""
        reference = self.validate_image(data, declared_type)
        target = self._path_for(reference["id"])
        if not target.exists():
            # Content-addressed: identical bytes are already there, and
            # rewriting would be work with no effect.
            write_file_atomic(target, bytes(data))
        return reference

    async def read_image(self, attachment_id: str) -> bytes:
        """Read, verifying the bytes still hash to their address (I2)."""
        target = self._path_for(attachment_id)
        if not target.exists():
            raise AttachmentError(
                "attachment-not-found", f"no attachment {attachment_id!r}"
            )
        data = target.read_bytes()
        if content_id(data) != attachment_id:
            # Swapped or corrupted. Serving it would hand back something other
            # than what the reference named, with nothing to reveal the change.
            raise AttachmentError(
                "attachment-corrupt",
                f"the stored bytes for {attachment_id!r} no longer match their address",
            )
        return data


__all__ = [
    "AttachmentStore",
    "LocalAttachments",
    "AttachmentError",
    "content_id",
    "ID_PREFIX",
    "DEFAULT_MAX_IMAGE_BYTES",
    "DEFAULT_ALLOWED_IMAGE_TYPES",
]
