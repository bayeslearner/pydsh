"""Provider adapters — the only part of pydsh that speaks to a network.

Everything below this package stops at the seam: `ctx.llm` resolves a route,
merges a call config, and hands a `GenerateOptions` to an adapter. These are
the adapters.

They are **provider-domain**, not core: mounted as plugins, swapped by config,
and never depended on by anything above. A consumer with its own provider
writes another one and mounts that instead.

The transport is a seam of its own, which is what lets every test here feed
real SSE bytes without opening a socket — the only honest way to test a
truncated stream. Its default implementation is httpx, imported lazily, so a
consumer bringing its own client installs nothing extra and one wanting the
batteries writes ``pydsh[http]``.
"""

from .deepseek import (
    CONTEXT_WINDOW_EXCEEDED,
    QUOTA_EXCEEDED,
    REASONING_EFFORTS,
    DeepSeek,
    DeepSeekAdapter,
    error_detail,
    http_error_code,
    is_context_window_exceeded,
    is_quota_exceeded,
    request_id,
    resolve_thinking,
    retry_after_seconds,
)
from .openai_compatible import (
    COMPLETIONS_PATH,
    DATA_PREFIX,
    DEFAULT_PROVIDERS,
    DONE,
    FINISH_REASONS,
    NO_OUTPUT,
    OpenAICompatible,
    OpenAICompatibleAdapter,
    ProviderConfig,
    map_finish_reason,
    map_usage,
    merge_providers,
    serialize_messages,
    serialize_request,
    translate,
)
from .transport import (
    DEFAULT_TIMEOUT_SECONDS,
    Transport,
    TransportUnavailable,
    aborted,
    httpx_transport,
    resolve_transport,
    with_idle_timeout,
)

__all__ = [
    # the dialect
    "OpenAICompatible",
    "OpenAICompatibleAdapter",
    "ProviderConfig",
    "DEFAULT_PROVIDERS",
    "merge_providers",
    "serialize_messages",
    "serialize_request",
    "translate",
    "map_finish_reason",
    "map_usage",
    "FINISH_REASONS",
    "DONE",
    "DATA_PREFIX",
    "NO_OUTPUT",
    "COMPLETIONS_PATH",
    # deepseek
    "DeepSeek",
    "DeepSeekAdapter",
    "http_error_code",
    "is_quota_exceeded",
    "is_context_window_exceeded",
    "error_detail",
    "retry_after_seconds",
    "request_id",
    "resolve_thinking",
    "CONTEXT_WINDOW_EXCEEDED",
    "QUOTA_EXCEEDED",
    "REASONING_EFFORTS",
    # the transport seam
    "Transport",
    "httpx_transport",
    "resolve_transport",
    "with_idle_timeout",
    "aborted",
    "TransportUnavailable",
    "DEFAULT_TIMEOUT_SECONDS",
]
