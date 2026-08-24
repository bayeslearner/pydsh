"""Call config — the epoch-level request configuration and its merge.

A call config is what a conversation is *currently* calling: provider, model,
and sampling. It is deliberately more stable than a single request's options,
because model routing is session-level state — letting it drift silently per
call is how a conversation ends up half-answered by two different models.

The merge has three layers, lowest first::

    provider defaults  <  session header  <  this request

A layer only contributes fields it actually set; ``None`` never overrides a
value a lower layer established (invariant I2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

#: The fields a layer may contribute, in declaration order.
CONFIG_FIELDS = (
    "provider",
    "model",
    "reasoning_effort",
    "temperature",
    "max_tokens",
    "stop",
)


@dataclass(frozen=True)
class LlmCallConfig:
    """The resolved configuration of one conversation epoch."""

    provider: str = ""
    model: str = ""
    reasoning_effort: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stop: Optional[tuple[str, ...]] = None


def call_config_equals(a: LlmCallConfig, b: LlmCallConfig) -> bool:
    """Field-by-field equality, comparing ``stop`` element-wise."""
    return (
        a.provider == b.provider
        and a.model == b.model
        and a.reasoning_effort == b.reasoning_effort
        and a.temperature == b.temperature
        and a.max_tokens == b.max_tokens
        and a.stop == b.stop
    )


def _normalize_stop(stop: Any) -> Optional[tuple[str, ...]]:
    """Accept a list or tuple of stop words; keep ``None`` as ``None``."""
    if stop is None:
        return None
    if isinstance(stop, (list, tuple)):
        return tuple(stop)
    raise TypeError(f"stop must be a sequence of strings, got {type(stop).__name__}")


def merge_call_config(
    provider_defaults: Optional[dict] = None,
    header: Optional[dict] = None,
    request: Optional[dict] = None,
) -> LlmCallConfig:
    """Merge the three layers, highest priority last.

    :param provider_defaults: what the adapter's route declares (lowest).
    :param header: the session's persisted epoch config.
    :param request: this call's own options (highest).
    """
    merged: dict[str, Any] = {}
    for layer in (provider_defaults or {}, header or {}, request or {}):
        for key, value in layer.items():
            if value is not None:
                merged[key] = value
    return LlmCallConfig(
        provider=merged.get("provider", ""),
        model=merged.get("model", ""),
        reasoning_effort=merged.get("reasoning_effort"),
        temperature=merged.get("temperature"),
        max_tokens=merged.get("max_tokens"),
        stop=_normalize_stop(merged.get("stop")),
    )


def call_config_to_dict(config: LlmCallConfig) -> dict:
    """Encode for the session header, omitting unset optional fields."""
    out: dict[str, Any] = {"provider": config.provider, "model": config.model}
    if config.reasoning_effort is not None:
        out["reasoning_effort"] = config.reasoning_effort
    if config.temperature is not None:
        out["temperature"] = config.temperature
    if config.max_tokens is not None:
        out["max_tokens"] = config.max_tokens
    if config.stop is not None:
        out["stop"] = list(config.stop)
    return out


def call_config_from_options(options: Any) -> dict:
    """Extract the persistable call-config fields from a request's options."""
    # An unset provider/model is the empty string on GenerateOptions, not
    # None. Emitting it would let a blank request erase the route's default
    # model, so treat empty as "this layer did not set it" (invariant I2).
    out: dict[str, Any] = {}
    if options.provider:
        out["provider"] = options.provider
    if options.model:
        out["model"] = options.model
    for field in ("reasoning_effort", "temperature", "max_tokens"):
        value = getattr(options, field, None)
        if value is not None:
            out[field] = value
    stop = getattr(options, "stop", None)
    if stop is not None:
        out["stop"] = list(stop)
    return out


__all__ = [
    "LlmCallConfig",
    "CONFIG_FIELDS",
    "merge_call_config",
    "call_config_equals",
    "call_config_to_dict",
    "call_config_from_options",
]
