"""App attribution — Requirement 5 and invariant I3 (no secrets in headers)."""

from __future__ import annotations

import re

from pydsh.llm import AppIdentity, attribution_headers, default_identity, user_agent

#: RFC 9110 product/comment: ``product/version (+url)``.
USER_AGENT_RE = re.compile(r"^[A-Za-z0-9._-]+/\S+ \(\+\S+\)$")


def test_user_agent_has_the_product_comment_shape():
    assert USER_AGENT_RE.match(user_agent())


def test_header_name_is_lowercase():
    assert list(attribution_headers()) == ["user-agent"]


def test_custom_identity_is_used_verbatim():
    identity = AppIdentity(product="acme-bot", version="9.9", url="https://acme.test")
    assert user_agent(identity) == "acme-bot/9.9 (+https://acme.test)"


def test_omitting_identity_falls_back_rather_than_suppressing():
    """Requirement 5.3 — attribution is mandatory; there is no 'off'."""
    headers = attribution_headers(None)
    assert headers["user-agent"].startswith("pydsh/")


def test_version_comes_from_package_metadata():
    """Requirement 5.2 — read, never hand-copied."""
    from importlib.metadata import version

    assert default_identity().version == version("pydsh")


def test_identity_is_stable_across_calls():
    """No per-request data may influence the value."""
    assert user_agent() == user_agent()


def test_headers_carry_no_secrets():
    """Invariant I3, asserted rather than assumed."""
    blob = " ".join(attribution_headers().values()).lower()
    for forbidden in ("sk-", "bearer", "token", "/users/", "session", "api_key"):
        assert forbidden not in blob


def test_identity_is_immutable():
    identity = default_identity()
    try:
        identity.product = "spoofed"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("AppIdentity must be frozen")
