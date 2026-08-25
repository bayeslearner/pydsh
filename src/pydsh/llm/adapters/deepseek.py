"""The DeepSeek dialect — the same wire, a richer failure vocabulary.

DeepSeek speaks OpenAI-compatible `/chat/completions`, so this **extends**
rather than copies. The reference duplicates the whole serializer here, which
is how it ended up with two copies of the tool-result ordering bug; a subclass
has one.

What is genuinely different is the failure side. A 400 that says "maximum
context length" and a 400 that says "unknown parameter" call for completely
different responses — compact and retry, versus fix the request — and only one
of them is worth retrying. So the status alone is not the classification: the
body is read too, and the result is a stable code the retry policy above can
decide on without parsing English.

Also different: **thinking**. `reasoning_effort: "off"` is not a wire value;
turning reasoning off is `thinking: {"type": "disabled"}`, and sending the two
together is contradictory.
"""

from __future__ import annotations

import email.utils
import re
import time
from typing import Any, Optional

from ..errors import LlmError
from .transport import with_idle_timeout
from .openai_compatible import (
    DEFAULT_PROVIDERS,
    OpenAICompatible,
    OpenAICompatibleAdapter,
    ProviderConfig,
    serialize_request,
)

#: What DeepSeek's own models hold, when config says nothing.
DEFAULT_CONTEXT_WINDOW = 1_000_000
DEFAULT_MAX_TOKENS = 256_000

#: Stable codes this adapter adds beyond the shared set.
CONTEXT_WINDOW_EXCEEDED = "CONTEXT_WINDOW_EXCEEDED"
QUOTA_EXCEEDED = "QUOTA"

#: The reasoning levels the wire accepts. ``off`` is not one of them — it is
#: expressed as `thinking: disabled` — which is exactly the distinction that
#: makes this a resolution step rather than a pass-through.
REASONING_EFFORTS = ("off", "high", "max")

#: How long one read may stall before the stream is declared dead. An endpoint
#: that accepts a connection and then goes quiet would otherwise hold a turn
#: open indefinitely, which looks like a slow model and is not one.
DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 300.0

_CONTEXT_WINDOW_PATTERNS = (
    re.compile(
        r"\b(?:maximum|max)(?:\s+(?:allowed|supported))?\s+context\s+(?:length|window)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:input|prompt|request)\s+(?:is\s+)?too\s+(?:long|large)\s+for\s+(?:this|the)\s+model\b",
        re.I,
    ),
    re.compile(
        r"\b(?:context|token)[\s_-]+(?:length|window)[\s_-]+(?:exceeded|too[\s_-]+long)\b",
        re.I,
    ),
)

_QUOTA_PATTERNS = (
    re.compile(r"\binsufficient[\s_-]+(?:quota|balance|credits?)\b", re.I),
    re.compile(r"\b(?:quota|usage[\s_-]+limit)[\s_-]+(?:exceeded|exhausted|reached)\b", re.I),
    # The copula is optional: "credits exhausted" and "your credits are
    # exhausted" are the same message, and the reference's pattern matches only
    # the first. A heuristic over free text that misses the more natural
    # phrasing classifies a spent account as a rate limit — which is retried,
    # forever.
    re.compile(
        r"\b(?:balance|credits?)[\s_-]+(?:(?:is|are|was|were|has|have)[\s_-]+"
        r"(?:been[\s_-]+)?)?(?:exhausted|depleted)\b",
        re.I,
    ),
    re.compile(r"\bout[\s_-]+of[\s_-]+(?:credits?|budget)\b", re.I),
)


def is_context_window_exceeded(detail: str) -> bool:
    """Does this error text mean the request was too long for the model?"""
    return any(pattern.search(detail or "") for pattern in _CONTEXT_WINDOW_PATTERNS)


def is_quota_exceeded(detail: str) -> bool:
    """Does this mean the account is out of credit, rather than rate-limited?

    Worth telling apart: a rate limit clears by waiting, and a spent quota does
    not. Retrying the second one is a loop that never ends.
    """
    return any(pattern.search(detail or "") for pattern in _QUOTA_PATTERNS)


def error_detail(error: Optional[dict]) -> str:
    """The searchable text of a provider error body."""
    error = error or {}
    return " ".join(
        str(part)
        for part in (error.get("code"), error.get("type"), error.get("message"))
        if part
    )


def http_error_code(status: int, error: Optional[dict] = None) -> str:
    """Classify one failure into a stable code.

    Body before status where it matters: a spent quota arrives as a 429 that is
    indistinguishable by status from an ordinary rate limit, and the two want
    opposite responses.
    """
    if status in (401, 403):
        return "AUTH"
    detail = error_detail(error)
    if is_quota_exceeded(detail):
        return QUOTA_EXCEEDED
    if status == 429:
        return "RATE_LIMIT"
    if status == 400:
        return (
            CONTEXT_WINDOW_EXCEEDED
            if is_context_window_exceeded(detail)
            else "INVALID_REQUEST"
        )
    if status >= 500:
        return "SERVER"
    return f"HTTP_{status}"


def retry_after_seconds(value: Optional[str], now: Optional[float] = None) -> Optional[float]:
    """Parse a `retry-after` header — plain seconds or an HTTP date.

    A delay already in the past comes back as ``None``: it says "retry now",
    and returning a negative number would have a caller sleeping on a
    nonsensical value.
    """
    if not value:
        return None
    text = value.strip()
    if re.fullmatch(r"\d+", text):
        delay = float(text)
        return delay if delay > 0 else None
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    delay = parsed.timestamp() - (now if now is not None else time.time())
    return delay if delay > 0 else None


def request_id(headers: Any) -> Optional[str]:
    """The provider's request id, for quoting in a bug report."""
    headers = headers or {}
    return headers.get("x-request-id") or headers.get("x-deepseek-request-id") or None


def resolve_thinking(effort: Optional[str]) -> dict:
    """Turn a reasoning effort into the wire's two separate fields.

    ``off`` is not a `reasoning_effort` value; it is `thinking: disabled`.
    Sending both at once states two contradictory things about the same
    request, so exactly one of them is set.

    :raises LlmError: an effort the wire has no representation for.
    """
    if effort is None or effort == "":
        return {}
    if effort not in REASONING_EFFORTS:
        raise LlmError(
            f"reasoning effort {effort!r} is not supported; expected one of "
            f"{', '.join(REASONING_EFFORTS)}",
            "UNSUPPORTED_REASONING_EFFORT",
        )
    if effort == "off":
        return {"thinking": {"type": "disabled"}}
    return {"reasoning_effort": effort}


def serialize_deepseek_request(options: Any) -> dict:
    """The shared body, with thinking resolved onto it."""
    body = serialize_request(options)
    # The shared serializer passes `reasoning_effort` straight through, which
    # is right for a plain OpenAI endpoint and wrong here for `off`.
    body.pop("reasoning_effort", None)
    body.update(resolve_thinking(options.reasoning_effort))
    return body


class DeepSeekAdapter(OpenAICompatibleAdapter):
    """The OpenAI-compatible adapter, with DeepSeek's request and errors."""

    def __init__(self, *args: Any, idle_timeout: Optional[float] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.idle_timeout = (
            DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS if idle_timeout is None else idle_timeout
        )

    def serialize(self, options: Any) -> dict:
        return serialize_deepseek_request(options)

    def wrap_lines(self, lines: Any, options: Any) -> Any:
        if not self.idle_timeout:
            return lines
        return with_idle_timeout(lines, self.idle_timeout)

    async def stream(self, options: Any):
        try:
            async for chunk in super().stream(options):
                yield chunk
        except LlmError as error:
            raise self.reclassify(error) from error

    @staticmethod
    def reclassify(error: LlmError) -> LlmError:
        """Sharpen a generic transport failure into a DeepSeek code.

        The transport can only say `HTTP_400`; whether that is a context
        overflow or a malformed request is in the body it carries.
        """
        match = re.fullmatch(r"HTTP_(\d{3})", error.code or "")
        if match is None:
            return error
        status = int(match.group(1))
        code = http_error_code(status, {"message": error.message})
        if code == error.code:
            return error
        return LlmError(error.message, code, cause=error.cause or error)


def _deepseek_defaults() -> tuple[ProviderConfig, ...]:
    """The `deepseek` route from the shared table, with its own limits."""
    base = next(p for p in DEFAULT_PROVIDERS if p.provider == "deepseek")
    return (
        ProviderConfig(
            provider=base.provider,
            display_name=base.display_name,
            base_url=base.base_url,
            api_key_ref=base.api_key_ref,
            allow_empty_key=base.allow_empty_key,
            context_window=DEFAULT_CONTEXT_WINDOW,
            max_tokens=DEFAULT_MAX_TOKENS,
        ),
    )


class DeepSeek(OpenAICompatible):
    """Registers the DeepSeek adapter on the `deepseek` route.

    Mount this *instead of* :class:`OpenAICompatible` when DeepSeek is the
    provider, or alongside it with that route overridden — plugkit's route
    binding is all-or-nothing, so two plugins claiming one provider is an error
    rather than a silent last-one-wins.
    """

    provide = "deepseek"
    adapter_class = DeepSeekAdapter
    default_providers = _deepseek_defaults()


__all__ = [
    "DeepSeek",
    "DeepSeekAdapter",
    "http_error_code",
    "is_quota_exceeded",
    "is_context_window_exceeded",
    "error_detail",
    "retry_after_seconds",
    "request_id",
    "resolve_thinking",
    "serialize_deepseek_request",
    "CONTEXT_WINDOW_EXCEEDED",
    "QUOTA_EXCEEDED",
    "REASONING_EFFORTS",
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS",
]
