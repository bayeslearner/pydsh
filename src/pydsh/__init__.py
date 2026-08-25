"""pydsh — a Python port of the DeepSeek Harness service layer on plugkit.

Three things are mounted as plugkit services, and a consumer reaches each by
name on its context:

- ``ctx.sessions`` — the append-only session log and its SQLite persistence.
- ``ctx.llm`` — the adapter registry and the interceptable model stream.
- ``ctx.token_meter`` — one estimator for conversation pressure.
- ``ctx.agents`` / ``ctx.agent_loop`` — the turn/step loop that drives a
  conversation, held behind a registry so it can be replaced.

Mount them onto a root context and they are available everywhere below it::

    from plugkit import Context
    from pydsh import AgentLoop, AgentRegistry, LlmService, SessionStore, TokenMeter

    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(LlmService)
    await root.plugin(TokenMeter)
    await root.plugin(AgentRegistry)
    await root.plugin(AgentLoop)

The loop reaches tools through plugkit's ``ctx.tools`` when it is mounted, and
offers the model none when it is not.

The shared conversation vocabulary lives in :mod:`pydsh.message` and is what
every seam speaks. Provider adapters are plugins mounted above this layer, not
part of it — nothing here opens a socket.
"""

from .agent import (
    Agent,
    AgentLoop,
    AgentOptions,
    AgentRegistry,
    BlockAssembler,
    Inbox,
)
from .cancel import CancelledError, CancelSignal
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
from .prompt import (
    PromptAssembly,
    PromptContext,
    PromptSection,
    SystemPrompt,
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
from .governance import (
    HooksProtocol,
    InvariantRegistry,
    ScheduleError,
    ScheduleRuntime,
    merge_hook_outputs,
)
from .query import (
    QueryError,
    SessionQueryEngine,
    SessionReferences,
    decode_reference_uri,
    encode_reference_uri,
)
from .work import (
    GoalError,
    GoalService,
    GoalTool,
    JobTools,
    LocalJobs,
)
from .tools import (
    BashTool,
    FsTools,
    RepeatToolGuard,
    SpillPolicy,
    SystemInstructions,
    TerminalTool,
    TimeContext,
    TodoTool,
)
from .compaction import (
    BasicCompaction,
    CompactionEngine,
    CompactionRefused,
    CompactionResult,
)
from .bounded import (
    ItemRetainer,
    LocalSpillStore,
    SpillStore,
    TextRetainer,
    ToolResultPruner,
    format_retention_notice,
)
from .capability import (
    Deadline,
    FileSystem,
    IdleWatchdog,
    ShellService,
    TerminalService,
    TimeoutReason,
    clamp_timeout,
    deadline,
)
from .operating import (
    AnonymousUserId,
    CommandInvocation,
    CommandResult,
    Commands,
    Credentials,
    Settings,
)
from .storage import (
    Domain,
    DomainSpec,
    JsonStorage,
    SqliteStorage,
    Storage,
    StorageDomain,
    StorageError,
    define_domain,
    domain_table,
)
from .session import (
    CheckpointPolicy,
    ProjectionCache,
    ProjectionDefinition,
    Session,
    SessionEvent,
    SessionHeader,
    SessionPersistence,
    SessionProjections,
    SessionStats,
    SessionStore,
    SqliteSessionPersistence,
)


def _resolve_version() -> str:
    """Read the version from installed metadata — pyproject.toml owns it.

    Hardcoding it here forks the fact: the two copies drift the first time one
    is bumped alone, which is exactly what happened between 0.2.0 and 0.2.1.
    """
    try:
        from importlib.metadata import version

        return version("pydsh")
    except Exception:  # noqa: BLE001 - running from a source tree, uninstalled
        return "0.0.0+unknown"


__version__ = _resolve_version()

__all__ = [
    "__version__",
    # session seam
    "SessionStore",
    "Session",
    "SessionEvent",
    "SessionHeader",
    "SessionPersistence",
    "SqliteSessionPersistence",
    # projections + durability
    "SessionProjections",
    "ProjectionDefinition",
    "SessionStats",
    "CheckpointPolicy",
    # governance
    "ScheduleRuntime",
    "ScheduleError",
    "HooksProtocol",
    "merge_hook_outputs",
    "InvariantRegistry",
    # reading history
    "SessionQueryEngine",
    "SessionReferences",
    "QueryError",
    "encode_reference_uri",
    "decode_reference_uri",
    # jobs and goals
    "LocalJobs",
    "JobTools",
    "GoalService",
    "GoalTool",
    "GoalError",
    # default tools
    "FsTools",
    "BashTool",
    "TerminalTool",
    "TodoTool",
    "RepeatToolGuard",
    "SpillPolicy",
    "TimeContext",
    "SystemInstructions",
    # compaction
    "CompactionEngine",
    "CompactionResult",
    "CompactionRefused",
    "BasicCompaction",
    # bounded output
    "ItemRetainer",
    "TextRetainer",
    "format_retention_notice",
    "SpillStore",
    "LocalSpillStore",
    "ToolResultPruner",
    # capability seams
    "FileSystem",
    "ShellService",
    "TerminalService",
    "TimeoutReason",
    "Deadline",
    "deadline",
    "clamp_timeout",
    "IdleWatchdog",
    # operating core
    "Settings",
    "Credentials",
    "Commands",
    "CommandInvocation",
    "CommandResult",
    "AnonymousUserId",
    "ProjectionCache",
    # storage seam
    "Storage",
    "JsonStorage",
    "SqliteStorage",
    "StorageDomain",
    "Domain",
    "DomainSpec",
    "define_domain",
    "domain_table",
    "StorageError",
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
    # agent seam
    "AgentRegistry",
    "AgentLoop",
    "Agent",
    "AgentOptions",
    "Inbox",
    "BlockAssembler",
    # cancellation
    "CancelSignal",
    "CancelledError",
    # system prompt
    "SystemPrompt",
    "PromptSection",
    "PromptContext",
    "PromptAssembly",
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
