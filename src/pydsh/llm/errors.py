"""The seam's error type and API-key hygiene.

Adapters raise :class:`LlmError` carrying a **stable code**, never a bare
provider exception. The code is what the retry policy decides on, so it must
not depend on parsing an error message.
"""

from __future__ import annotations

import re
from typing import Any

#: Stable codes every adapter maps its provider's failures onto. The retry
#: policy's default retryable set is drawn from these.
ERROR_CODES = (
    "EMPTY_RESPONSE",
    "RATE_LIMIT",
    "SERVER",
    "TIMEOUT",
    "TRANSPORT",
    "AUTH",
    "INVALID_REQUEST",
    "UNKNOWN",
)


class LlmError(Exception):
    """An adapter failure with a stable, matchable code."""

    def __init__(self, message: str, code: str = "UNKNOWN", cause: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.cause = cause

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


# A legal API key is printable ASCII with no spaces.
_LEGAL_API_KEY = re.compile(r"^[\x21-\x7E]+$")


def normalize_api_key(raw: str) -> tuple[str, str]:
    """Validate a supplied API key, trimming surrounding whitespace first.

    Returns ``(verdict, value)`` where verdict is ``ok`` / ``empty`` /
    ``illegal``. Only an ``ok`` verdict carries the key back; a rejected key is
    never echoed, so a malformed secret cannot reach a log through this path.
    """
    value = raw.strip()
    if not value:
        return "empty", ""
    if not _LEGAL_API_KEY.fullmatch(value):
        return "illegal", ""
    return "ok", value


__all__ = ["LlmError", "normalize_api_key", "ERROR_CODES"]
