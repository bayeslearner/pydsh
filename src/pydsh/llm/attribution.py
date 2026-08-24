"""App attribution — the non-secret product identity every adapter sends.

Every HTTP-shaped adapter sends a static ``User-Agent`` on each provider
request, in RFC 9110 product/comment form: ``product/version (+url)``.

The fields are **public product facts only** (invariant I3). No key, filesystem
path, session id, prompt text, or per-user identifier may appear here, and no
per-request data influences the value. A white-label deployment overrides the
identity; omitting one falls back to the pydsh default rather than suppressing
attribution.

This module is the single authority for that default — an adapter never
hand-copies a version constant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

_APP_VERSION: Optional[str] = None


def _resolve_version() -> str:
    """Read the installed package version; fall back if not installed."""
    global _APP_VERSION
    if _APP_VERSION is not None:
        return _APP_VERSION
    try:
        from importlib.metadata import version

        _APP_VERSION = version("pydsh")
    except Exception:  # noqa: BLE001 - any resolution failure falls back
        _APP_VERSION = "0.0.0+unknown"
    return _APP_VERSION


@dataclass(frozen=True)
class AppIdentity:
    """A static, public application identity sent to LLM providers."""

    product: str
    version: str
    url: str


def default_identity() -> AppIdentity:
    """pydsh's own identity — the fallback every adapter gets for free."""
    return AppIdentity(
        product="pydsh",
        version=_resolve_version(),
        url="https://github.com/bayeslearner/pydsh",
    )


def user_agent(identity: Optional[AppIdentity] = None) -> str:
    """The ``User-Agent`` value: ``product/version (+url)``."""
    ident = identity or default_identity()
    return f"{ident.product}/{ident.version} (+{ident.url})"


def attribution_headers(identity: Optional[AppIdentity] = None) -> dict[str, str]:
    """The headers an adapter must send on every provider request.

    Header names are lowercase — HTTP field names are case-insensitive on the
    wire, and a single casing keeps adapter tests from asserting two spellings.
    """
    return {"user-agent": user_agent(identity)}


__all__ = [
    "AppIdentity",
    "default_identity",
    "user_agent",
    "attribution_headers",
]
