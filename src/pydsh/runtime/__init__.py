"""The runtime — the same SDK surface, one process away.

Sprint 20's `Harness` runs a conversation in *this* process. Sometimes that is
the wrong process: a long-lived runtime a short-lived client talks to, a
sandbox boundary, an editor plugin that must not import the whole package.

Newline-delimited JSON-RPC 2.0 in both directions::

    async with RuntimeClient(provider="openai", model="gpt-4o") as client:
        result = await client.session("my-chat").run("what changed today?")

The client spawns `python -m pydsh.runtime` unless given a transport of its
own. While a turn runs the server forwards each session event as a
`session.event` notification, so a caller sees the conversation as it happens
rather than as a lump at the end.
"""

from .client import (
    CHILD_GRACE_SECONDS,
    DEFAULT_HANDSHAKE_TIMEOUT,
    RUNTIME_MODULE,
    RemoteRunResult,
    RemoteSession,
    RuntimeClient,
)
from .protocol import (
    INTERNAL_ERROR,
    METHOD_NOT_FOUND,
    REQUEST_ID_PREFIX,
    JsonRpcError,
    JsonRpcTransport,
    MethodNotFound,
    Reader,
    TransportClosed,
    Writer,
    duplex,
    pipe,
    stdin_reader,
    stdout_writer,
)
from .server import (
    EVENT_NOTIFICATION,
    METHODS,
    PROTOCOL_VERSION,
    SERVER_NAME,
    STATUS_NOTIFICATION,
    RuntimeServer,
    blocks_from_wire,
    serve,
)

__all__ = [
    # the client
    "RuntimeClient",
    "RemoteSession",
    "RemoteRunResult",
    "RUNTIME_MODULE",
    "DEFAULT_HANDSHAKE_TIMEOUT",
    "CHILD_GRACE_SECONDS",
    # the server
    "RuntimeServer",
    "serve",
    "blocks_from_wire",
    "SERVER_NAME",
    "PROTOCOL_VERSION",
    "METHODS",
    "EVENT_NOTIFICATION",
    "STATUS_NOTIFICATION",
    # the transport
    "JsonRpcTransport",
    "JsonRpcError",
    "TransportClosed",
    "MethodNotFound",
    "Reader",
    "Writer",
    "stdin_reader",
    "stdout_writer",
    "pipe",
    "duplex",
    "REQUEST_ID_PREFIX",
    "METHOD_NOT_FOUND",
    "INTERNAL_ERROR",
]
