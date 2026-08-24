"""Retry policy resolution and backoff — Requirement 3."""

from __future__ import annotations

import pytest

from pydsh.llm import (
    DEFAULT_RETRYABLE_CODES,
    ResolvedRetryPolicy,
    RetryPolicyError,
    resolve_retry_policy,
)


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def test_absent_config_resolves_to_normal_defaults():
    policy = resolve_retry_policy(None)
    assert policy.mode == "normal"
    assert policy.retryable_codes == DEFAULT_RETRYABLE_CODES


def test_full_config_is_honoured():
    policy = resolve_retry_policy(
        {
            "mode": "always",
            "maxRetries": 7,
            "retryableCodes": ["SERVER"],
            "backoff": {
                "initialDelayMs": 10,
                "maxDelayMs": 100,
                "jitterRatio": 0.5,
            },
        }
    )
    assert policy.mode == "always"
    assert policy.max_retries == 7
    assert policy.retryable_codes == ("SERVER",)
    assert (policy.initial_delay_ms, policy.max_delay_ms) == (10, 100)


@pytest.mark.parametrize(
    "config,match",
    [
        ({"nope": 1}, "unknown field"),
        ({"mode": "sometimes"}, "mode must be"),
        ({"maxRetries": -1}, "non-negative"),
        ({"maxRetries": 1.5}, "non-negative"),
        ({"maxRetries": True}, "non-negative"),
        ({"retryableCodes": []}, "non-empty"),
        ({"retryableCodes": ["A", "A"]}, "must not repeat"),
        ({"retryableCodes": "SERVER"}, "non-empty array"),
        ({"backoff": {"initialDelayMs": 0}}, "positive integer"),
        ({"backoff": {"maxDelayMs": -5}}, "positive integer"),
        ({"backoff": {"initialDelayMs": 900, "maxDelayMs": 100}}, "must not exceed"),
        ({"backoff": {"jitterRatio": 2}}, r"\[0, 1\]"),
        ({"backoff": {"jitterRatio": "x"}}, "must be a number"),
        ({"backoff": 5}, "must be an object"),
    ],
)
def test_illegal_config_fails_loudly(config, match):
    """A bad policy breaks the mount, not the first call hours later."""
    with pytest.raises(RetryPolicyError, match=match):
        resolve_retry_policy(config)


def test_error_message_names_the_config_path():
    with pytest.raises(RetryPolicyError, match="providers.acme.retry"):
        resolve_retry_policy({"mode": "bogus"}, path="providers.acme.retry")


# --------------------------------------------------------------------------- #
# should_retry
# --------------------------------------------------------------------------- #
def test_normal_mode_retries_only_listed_codes():
    policy = ResolvedRetryPolicy(mode="normal", max_retries=3)
    assert policy.should_retry("SERVER", 0)
    assert not policy.should_retry("AUTH", 0)


def test_normal_mode_stops_at_max_retries():
    policy = ResolvedRetryPolicy(mode="normal", max_retries=2)
    assert policy.should_retry("SERVER", 1)
    assert not policy.should_retry("SERVER", 2)


def test_zero_max_retries_never_retries():
    assert not ResolvedRetryPolicy(mode="normal", max_retries=0).should_retry(
        "SERVER", 0
    )


def test_always_mode_retries_anything_forever():
    policy = ResolvedRetryPolicy(mode="always", max_retries=0)
    assert policy.should_retry("AUTH", 9999)


# --------------------------------------------------------------------------- #
# Backoff
# --------------------------------------------------------------------------- #
def test_backoff_grows_exponentially_within_jitter_bounds():
    policy = ResolvedRetryPolicy(
        mode="normal", initial_delay_ms=100, max_delay_ms=10_000, jitter_ratio=0.1
    )
    for attempt, base_ms in [(1, 100), (2, 200), (3, 400)]:
        delay = policy.delay_for(attempt)
        assert (base_ms * 0.9) / 1000 <= delay <= (base_ms * 1.1) / 1000


def test_backoff_is_capped():
    policy = ResolvedRetryPolicy(
        mode="normal", initial_delay_ms=100, max_delay_ms=250, jitter_ratio=0.0
    )
    assert policy.delay_for(20) == pytest.approx(0.25)


def test_zero_jitter_is_deterministic():
    policy = ResolvedRetryPolicy(
        mode="normal", initial_delay_ms=100, max_delay_ms=10_000, jitter_ratio=0.0
    )
    assert policy.delay_for(1) == pytest.approx(0.1)
    assert policy.delay_for(3) == pytest.approx(0.4)


def test_backoff_is_never_negative():
    policy = ResolvedRetryPolicy(
        mode="normal", initial_delay_ms=1, max_delay_ms=2, jitter_ratio=1.0
    )
    assert all(policy.delay_for(n) >= 0 for n in range(1, 20))
