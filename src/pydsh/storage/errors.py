"""The storage vocabulary of failures.

``code`` is the contract and ``message`` is the diagnosis. A caller matches on
the code — it is stable across every backend and every message rewording — and
reads the message to find out what actually happened.

Backend failures pass *through* the domain layer unwrapped. The domain adds
meaning to values, not a second exception hierarchy over the same failure.
"""

from __future__ import annotations

from typing import Any, Optional

#: Every code this seam raises. Listed together because they are the contract:
#: a consumer switching on `err.code` needs to know the whole set.
STORAGE_ERROR_CODES = (
    "backend-not-found",  # asked for a backend nobody registered
    "form-not-mounted",  # asked for a data form nobody mounted
    "duplicate-backend",  # two backends claimed one name
    "duplicate-mount",  # two facilities claimed one form
    "duplicate-domain",  # a domain is already open
    "version-mismatch",  # the medium holds another version of this unit
    "malformed-medium",  # the medium is unreadable or the wrong shape
    "closed",  # the unit or domain has been closed
    "invalid-record",  # a stored value no longer satisfies its schema
    "missing-key",  # updating a record that is not there
    "no-facet",  # the backend cannot serve this data shape
)


class StorageError(Exception):
    """A storage failure, identified by a stable code."""

    def __init__(self, code: str, message: str, cause: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        if cause is not None:
            self.__cause__ = cause

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"{type(self).__name__}({self.code!r}, {self.message!r})"


class DomainError(StorageError):
    """A domain-layer failure, with the table and key when it has them."""

    def __init__(
        self,
        code: str,
        message: str,
        detail: Optional[dict] = None,
        cause: Any = None,
    ) -> None:
        super().__init__(code, message, cause)
        self.detail = detail or {}


__all__ = ["StorageError", "DomainError", "STORAGE_ERROR_CODES"]
