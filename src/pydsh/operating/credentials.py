"""``ctx.credentials`` — config carries a *reference*, never a secret.

A deployment writes ``api_key_ref: "DEEPSEEK_API_KEY"``. The value behind that
name is resolved at the moment of use, from an explicit store first and the
environment second. Resolving per call rather than at startup is what lets a
rotated key take effect without a restart.

:meth:`Credentials.describe` deliberately does **not** return the value. It
exists to be shown — in a status line, a log, an error message — and a call
that is safe to display cannot carry the secret, or the first person to log its
output leaks the key.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

from plugkit import Service

from ..dispatch import emit_contained

#: A legal ref: an environment-variable-shaped identifier, so a ref can never
#: name something outside that namespace.
REF_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Broadcast when a stored credential changes, so a holder can re-resolve.
CREDENTIALS_UPDATED = "credentials/updated"


class CredentialRefError(ValueError):
    """A ref that is not a safe identifier."""


def credential_ref(value: str) -> str:
    """Check a ref's shape and return it unchanged."""
    if not isinstance(value, str) or not REF_PATTERN.fullmatch(value):
        raise CredentialRefError(
            f"credential ref {value!r} is not a legal name "
            f"(it must match {REF_PATTERN.pattern})"
        )
    return value


class Credentials(Service):
    """Provides ``ctx.credentials``."""

    provide = "credentials"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._store: dict[str, str] = {}

    async def resolve(self, ref: str) -> Optional[dict]:
        """The value behind a ref and where it came from, or ``None``.

        The explicit store wins over the environment: a value set at runtime is
        a deliberate override, and the environment is the fallback beneath it.
        """
        credential_ref(ref)
        if ref in self._store:
            return {"value": self._store[ref], "source": "store"}
        from_env = os.environ.get(ref)
        if from_env is not None:
            return {"value": from_env, "source": "env"}
        return None

    async def set(self, ref: str, value: str) -> None:
        """Store a credential and announce that it changed."""
        credential_ref(ref)
        self._store[ref] = value
        emit_contained(self.ctx, CREDENTIALS_UPDATED, ref)

    async def delete(self, ref: str) -> bool:
        """Remove a stored credential. Never touches the environment."""
        credential_ref(ref)
        if ref not in self._store:
            return False
        del self._store[ref]
        emit_contained(self.ctx, CREDENTIALS_UPDATED, ref)
        return True

    async def describe(self, ref: str) -> dict:
        """Whether a ref resolves, and from where. **Never the value.**"""
        credential_ref(ref)
        resolved = await self.resolve(ref)
        if resolved is None:
            return {"ref": ref, "available": False}
        return {"ref": ref, "available": True, "source": resolved["source"]}


__all__ = [
    "Credentials",
    "credential_ref",
    "CredentialRefError",
    "REF_PATTERN",
    "CREDENTIALS_UPDATED",
]
