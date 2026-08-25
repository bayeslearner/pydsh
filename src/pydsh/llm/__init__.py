"""The LLM seam: adapters register, callers stream, middleware intercepts.

`ponytail:` no provider adapter ships here. ``openai_compatible``,
``deepseek`` and ``pi_ai`` are provider-domain plugins in a later sprint —
this package is the seam they plug into.
"""

from .adapter import LlmAdapter, LlmProviderInfo
from .attribution import (
    AppIdentity,
    attribution_headers,
    default_identity,
    user_agent,
)
from .call_config import (
    LlmCallConfig,
    call_config_equals,
    call_config_from_options,
    call_config_to_dict,
    merge_call_config,
)
from .chunks import ChunkType, GenerateOptions, StreamChunk, is_token_delta
from .adapters import (
    DeepSeek,
    DeepSeekAdapter,
    OpenAICompatible,
    OpenAICompatibleAdapter,
    PiAi,
    PiAiAdapter,
    ProfileError,
    ProviderConfig,
    Transport,
    httpx_transport,
)
from .errors import LlmError, normalize_api_key
from .retry import (
    DEFAULT_RETRYABLE_CODES,
    ResolvedRetryPolicy,
    RetryPolicyError,
    resolve_retry_policy,
)
from .service import (
    ADAPTERS_UPDATED,
    STREAM_WATERFALL,
    AdapterRegistration,
    LlmService,
)
from .token_meter import TokenMeter, estimate_text

__all__ = [
    # seam
    "LlmService",
    "AdapterRegistration",
    "LlmAdapter",
    "LlmProviderInfo",
    "ADAPTERS_UPDATED",
    "STREAM_WATERFALL",
    # protocol
    "ChunkType",
    "StreamChunk",
    "GenerateOptions",
    "is_token_delta",
    # errors
    "LlmError",
    "normalize_api_key",
    # provider adapters (provider-domain plugins)
    "OpenAICompatible",
    "OpenAICompatibleAdapter",
    "DeepSeek",
    "DeepSeekAdapter",
    "ProviderConfig",
    "PiAi",
    "PiAiAdapter",
    "ProfileError",
    "Transport",
    "httpx_transport",
    # call config
    "LlmCallConfig",
    "merge_call_config",
    "call_config_equals",
    "call_config_to_dict",
    "call_config_from_options",
    # retry
    "ResolvedRetryPolicy",
    "RetryPolicyError",
    "resolve_retry_policy",
    "DEFAULT_RETRYABLE_CODES",
    # attribution
    "AppIdentity",
    "default_identity",
    "user_agent",
    "attribution_headers",
    # metering
    "TokenMeter",
    "estimate_text",
]
