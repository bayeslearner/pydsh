"""pydsh — a Python port of the DeepSeek Harness service layer on plugkit.

Three things are mounted as plugkit services, and a consumer reaches each by
name on its context:

- ``ctx.sessions`` — the append-only session log and its SQLite persistence.
- ``ctx.llm`` — the adapter registry and the interceptable model stream.
- ``ctx.token_meter`` — one estimator for conversation pressure.

Mount them onto a root context and they are available everywhere below it::

    from plugkit import Context
    from pydsh import LlmService, SessionStore, TokenMeter

    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(LlmService)
    await root.plugin(TokenMeter)

The shared conversation vocabulary lives in :mod:`pydsh.message` and is what
every seam speaks. Provider adapters are plugins mounted above this layer, not
part of it — nothing here opens a socket.
"""

from .dispatch import emit_contained
from .llm import (
    AdapterRegistration,
    AppIdentity,
    ChunkType,
    GenerateOptions,
    LlmAdapter,
    LlmCallConfig,
    LlmError,
    LlmProviderInfo,
    LlmService,
    ResolvedRetryPolicy,
    RetryPolicyError,
    StreamChunk,
    TokenMeter,
    attribution_headers,
    merge_call_config,
    resolve_retry_policy,
)
from .message import (
    ContentBlock,
    Message,
    MessageSource,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    as_text,
    create_assistant_message,
    create_user_message,
    decode_payload,
    encode_payload,
)
from .session import (
    Session,
    SessionEvent,
    SessionHeader,
    SessionPersistence,
    SessionStore,
    SqliteSessionPersistence,
)

__version__ = "0.2.0"

__all__ = [
    "__version__",
    # session seam
    "SessionStore",
    "Session",
    "SessionEvent",
    "SessionHeader",
    "SessionPersistence",
    "SqliteSessionPersistence",
    # llm seam
    "LlmService",
    "LlmAdapter",
    "LlmProviderInfo",
    "AdapterRegistration",
    "ChunkType",
    "StreamChunk",
    "GenerateOptions",
    "LlmError",
    "LlmCallConfig",
    "merge_call_config",
    "ResolvedRetryPolicy",
    "RetryPolicyError",
    "resolve_retry_policy",
    "AppIdentity",
    "attribution_headers",
    # metering
    "TokenMeter",
    # vocabulary
    "Message",
    "MessageSource",
    "ContentBlock",
    "TextBlock",
    "ReasoningBlock",
    "ToolCallBlock",
    "ToolResultBlock",
    "as_text",
    "create_user_message",
    "create_assistant_message",
    "encode_payload",
    "decode_payload",
    # kernel helpers
    "emit_contained",
]
