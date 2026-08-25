"""Sidecars — what hangs off a conversation without being part of it.

Four services sharing one shape: each is *about* a conversation without being
*in* it. None appends to the surface, and that is the point — a picture, a
rating, a memory of an earlier session, and the way a client reaches any of
them are all things the model's history should not carry.

- ``ctx.attachments`` — immutable binary content under a content address, so a
  reference names the bytes rather than a place to find them.
- ``ctx.message_feedback`` — a durable opinion about a finished message, held
  beside the log rather than on it. An event would be on the surface, and the
  model would read the user's rating of its last answer as part of the
  conversation.
- ``ctx.typert`` — the declarative remote-call protocol: methods opt in, the
  registry collects them, a client invokes by name.
- ``ctx.long_term_memory`` — exchanges captured across sessions and recalled
  into a later one as history.
"""

from .attachments import (
    DEFAULT_ALLOWED_IMAGE_TYPES,
    DEFAULT_MAX_IMAGE_BYTES,
    ID_PREFIX,
    AttachmentError,
    AttachmentStore,
    LocalAttachments,
    content_id,
)
from .feedback import (
    DEFAULT_MAX_NOTE_BYTES,
    FEEDBACK_DOMAIN,
    RATINGS,
    FeedbackError,
    MessageFeedback,
    lifetime_identity,
)
from .memory import (
    DEFAULT_CAPTURE_TEXT_LIMIT,
    DEFAULT_MAX_INJECTED_CHARS,
    DEFAULT_MAX_RECALLED,
    DEFAULT_RECENT_COUNT,
    MEMORY_DOMAIN,
    PLUGIN_NAME,
    RECALL_FORM,
    RECALL_HEADER,
    LongTermMemory,
    build_recall,
    last_user_text,
    memory_key,
    tokenize,
)
from .typert import (
    REMOTE_ATTR,
    SCOPE_ATTR,
    WIRE_ATTR,
    InvocationDescriptor,
    RemoteFailure,
    RemoteResult,
    TypertRegistry,
    remote,
    remote_scope,
    scope_name_of,
)

__all__ = [
    # attachments
    "AttachmentStore",
    "LocalAttachments",
    "AttachmentError",
    "content_id",
    "ID_PREFIX",
    "DEFAULT_MAX_IMAGE_BYTES",
    "DEFAULT_ALLOWED_IMAGE_TYPES",
    # feedback
    "MessageFeedback",
    "FeedbackError",
    "FEEDBACK_DOMAIN",
    "lifetime_identity",
    "DEFAULT_MAX_NOTE_BYTES",
    "RATINGS",
    # typert
    "remote",
    "remote_scope",
    "scope_name_of",
    "TypertRegistry",
    "InvocationDescriptor",
    "RemoteResult",
    "RemoteFailure",
    "REMOTE_ATTR",
    "WIRE_ATTR",
    "SCOPE_ATTR",
    # memory
    "LongTermMemory",
    "MEMORY_DOMAIN",
    "RECALL_FORM",
    "RECALL_HEADER",
    "PLUGIN_NAME",
    "tokenize",
    "memory_key",
    "build_recall",
    "last_user_text",
    "DEFAULT_MAX_INJECTED_CHARS",
    "DEFAULT_MAX_RECALLED",
    "DEFAULT_RECENT_COUNT",
    "DEFAULT_CAPTURE_TEXT_LIMIT",
]
