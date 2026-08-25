"""The gateway — the runtime's method surface, over a socket.

One booted context, many clients, each with its own `RuntimeServer` and its own
event subscription. That last part is what keeps two clients from seeing each
other's conversations.

The transport is sprint 21's, unchanged: a WebSocket connection is adapted into
the reader and writer it already takes, rather than given a second
implementation of frame dispatch that could drift from the first.

It authenticates nobody, deliberately, and binds loopback by default.
"""

from .connection import MAX_FRAME_BYTES, FrameTooLarge, connection_io
from .server import (
    DEFAULT_HOST,
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_PORT,
    REFUSED_REASON,
    Gateway,
    serve,
)

__all__ = [
    "Gateway",
    "serve",
    "connection_io",
    "FrameTooLarge",
    "MAX_FRAME_BYTES",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_MAX_CONNECTIONS",
    "REFUSED_REASON",
]
