"""Per-route retry policy: which failures are worth another attempt, and when.

Two modes. ``normal`` retries only codes in the retryable set, at most
``max_retries`` times. ``always`` retries every failure until the call succeeds,
is cancelled, or its fiber unloads — the mode an operator picks when a
long-running agent must not die on a flaky provider.

Configuration is validated at resolve time, not at first failure: an illegal
policy should break the mount, not surface hours later as a silent no-retry.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional

#: Failures worth retrying by default — all transient by nature.
DEFAULT_RETRYABLE_CODES = (
    "EMPTY_RESPONSE",
    "RATE_LIMIT",
    "SERVER",
    "TIMEOUT",
    "TRANSPORT",
)

DEFAULT_MAX_RETRIES = 2
DEFAULT_INITIAL_DELAY_MS = 500
DEFAULT_MAX_DELAY_MS = 10_000
DEFAULT_JITTER_RATIO = 0.1

RETRY_MODES = ("normal", "always")


class RetryPolicyError(ValueError):
    """A retry policy configuration is illegal."""


@dataclass(frozen=True)
class ResolvedRetryPolicy:
    """An immutable retry policy, captured when a provider route registers."""

    mode: str
    max_retries: int = DEFAULT_MAX_RETRIES
    retryable_codes: tuple[str, ...] = DEFAULT_RETRYABLE_CODES
    initial_delay_ms: int = DEFAULT_INITIAL_DELAY_MS
    max_delay_ms: int = DEFAULT_MAX_DELAY_MS
    jitter_ratio: float = DEFAULT_JITTER_RATIO

    def should_retry(self, error_code: str, attempts: int) -> bool:
        """Whether this failure earns another attempt.

        :param attempts: how many retries have already been spent.
        """
        if self.mode == "always":
            return True
        return attempts < self.max_retries and error_code in self.retryable_codes

    def delay_for(self, attempt: int) -> float:
        """Seconds to wait before retry number ``attempt`` (1-based).

        Bounded exponential backoff with symmetric jitter. The cap applies
        before jitter, so the true maximum is ``max_delay_ms * (1 + jitter)``.
        """
        base = min(self.initial_delay_ms * (2 ** (attempt - 1)), self.max_delay_ms)
        jitter = 1.0 + random.uniform(-self.jitter_ratio, self.jitter_ratio)
        return (base * jitter) / 1000.0


def _validate_backoff(initial: int, maximum: int, jitter: float) -> None:
    """Reject a backoff that could never produce a sane delay."""
    if not isinstance(initial, int) or isinstance(initial, bool) or initial <= 0:
        raise RetryPolicyError("initialDelayMs must be a positive integer")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0:
        raise RetryPolicyError("maxDelayMs must be a positive integer")
    if initial > maximum:
        raise RetryPolicyError("initialDelayMs must not exceed maxDelayMs")
    if isinstance(jitter, bool) or not isinstance(jitter, (int, float)):
        raise RetryPolicyError("jitterRatio must be a number in [0, 1]")
    if not 0 <= jitter <= 1:
        raise RetryPolicyError("jitterRatio must be in [0, 1]")


def resolve_retry_policy(
    config: Optional[dict] = None, path: str = "retry"
) -> ResolvedRetryPolicy:
    """Resolve a provider's retry configuration into an immutable policy.

    Config shape (keys mirror the TypeScript reference)::

        {"mode": "normal"|"always", "maxRetries": n, "retryableCodes": [...],
         "backoff": {"initialDelayMs": n, "maxDelayMs": n, "jitterRatio": f}}

    :param path: config path used in error messages, so a bad policy points at
        itself rather than at "some retry config somewhere".
    :raises RetryPolicyError: on any unknown key or out-of-range value.
    """
    if config is None:
        return ResolvedRetryPolicy(mode="normal")

    allowed = {"mode", "maxRetries", "retryableCodes", "backoff"}
    unknown = set(config) - allowed
    if unknown:
        raise RetryPolicyError(f"{path}: unknown field(s) {sorted(unknown)}")

    mode = config.get("mode", "normal")
    if mode not in RETRY_MODES:
        raise RetryPolicyError(f"{path}.mode must be one of {list(RETRY_MODES)}")

    backoff = config.get("backoff") or {}
    if not isinstance(backoff, dict):
        raise RetryPolicyError(f"{path}.backoff must be an object")
    initial = backoff.get("initialDelayMs", DEFAULT_INITIAL_DELAY_MS)
    maximum = backoff.get("maxDelayMs", DEFAULT_MAX_DELAY_MS)
    jitter = backoff.get("jitterRatio", DEFAULT_JITTER_RATIO)
    _validate_backoff(initial, maximum, jitter)

    retryable = config.get("retryableCodes")
    if retryable is not None:
        if not isinstance(retryable, (list, tuple)) or not retryable:
            raise RetryPolicyError(f"{path}.retryableCodes must be a non-empty array")
        if len(set(retryable)) != len(retryable):
            raise RetryPolicyError(f"{path}.retryableCodes must not repeat a code")
        retryable = tuple(retryable)
    else:
        retryable = DEFAULT_RETRYABLE_CODES

    max_retries = config.get("maxRetries", DEFAULT_MAX_RETRIES)
    if (
        not isinstance(max_retries, int)
        or isinstance(max_retries, bool)
        or max_retries < 0
    ):
        raise RetryPolicyError(f"{path}.maxRetries must be a non-negative integer")

    return ResolvedRetryPolicy(
        mode=mode,
        max_retries=max_retries,
        retryable_codes=retryable,
        initial_delay_ms=initial,
        max_delay_ms=maximum,
        jitter_ratio=jitter,
    )


__all__ = [
    "ResolvedRetryPolicy",
    "RetryPolicyError",
    "resolve_retry_policy",
    "DEFAULT_RETRYABLE_CODES",
    "RETRY_MODES",
]
