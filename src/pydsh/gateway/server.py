"""The gateway — one booted context, many clients.

Each client gets its **own** `RuntimeServer` over its own transport, and that
is not an implementation detail. A server forwards session events to *its*
transport; one shared server would forward every client's conversation to every
client, which is a correctness problem and a confidentiality problem in the
same line of code.

The gateway authenticates nobody, and says so. A scheme nobody chose is worse
than none, because it looks like protection — termination, identity and TLS
belong to whatever fronts this.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from ..runtime.protocol import JsonRpcTransport
from ..runtime.server import RuntimeServer
from .connection import MAX_FRAME_BYTES, FrameTooLarge, connection_io

logger = logging.getLogger("pydsh.gateway")

#: Loopback by default: a careless start should not be an open port on a
#: network interface.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080

#: How many clients at once. Bounded so many connections cannot grow the
#: process without limit.
DEFAULT_MAX_CONNECTIONS = 64

#: What a refused client is told before the socket closes.
REFUSED_REASON = "this gateway is at its connection limit"


class Gateway:
    """Serves one context to many clients."""

    def __init__(
        self,
        ctx: Any,
        options: Any = None,
        *,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        max_frame_bytes: int = MAX_FRAME_BYTES,
    ) -> None:
        self.ctx = ctx
        self.options = options
        self.max_connections = max_connections
        self.max_frame_bytes = max_frame_bytes
        self._connections: dict = {}
        self._closed = False

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def handle(self, connection: Any, *_path: Any) -> None:
        """Serve one client until it goes away. The `websockets` handler shape."""
        if self._closed:
            await _refuse(connection, "this gateway is closed")
            return
        if len(self._connections) >= self.max_connections:
            await _refuse(connection, REFUSED_REASON)
            return

        read, write = connection_io(connection, self.max_frame_bytes)
        transport = JsonRpcTransport(read, write)
        # Its own server, its own subscription (I1).
        server = RuntimeServer(self.ctx, transport, self.options)
        self._connections[id(connection)] = (transport, server)
        transport.start()

        try:
            while not transport.closed and not transport.eof:
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            raise
        finally:
            # Everything this connection held (I2). Without it a gateway
            # accumulates subscriptions to a context that keeps calling them,
            # writing to sockets that are gone.
            await self._release(connection)

    async def _release(self, connection: Any) -> None:
        entry = self._connections.pop(id(connection), None)
        if entry is None:
            return
        transport, server = entry
        try:
            await server.shutdown()
        except Exception as error:  # noqa: BLE001 - teardown is best-effort
            logger.warning("gateway: shutting a connection's server down failed: %s", error)
        await transport.close("the client disconnected")

    async def close(self) -> None:
        """Stop serving and release every connection. Idempotent."""
        if self._closed:
            return
        self._closed = True
        for key in list(self._connections):
            transport, server = self._connections.pop(key)
            try:
                await server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            await transport.close("the gateway is closing")


async def _refuse(connection: Any, reason: str) -> None:
    """Tell a client why, then close. A silent close is unattributable."""
    try:
        send = connection.send(
            '{"jsonrpc":"2.0","method":"gateway.refused","params":'
            f'{{"reason":"{reason}"}}}}'
        )
        if hasattr(send, "__await__"):
            await send
    except Exception:  # noqa: BLE001
        pass
    try:
        result = connection.close()
        if hasattr(result, "__await__"):
            await result
    except Exception:  # noqa: BLE001
        pass


async def serve(
    ctx: Any,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    options: Any = None,
    **gateway_options: Any,
) -> tuple[Gateway, Any]:
    """Bind a socket and serve. Needs the ``ws`` extra.

    :returns: the gateway and the underlying server, so a caller can await or
        close either.
    """
    try:
        import websockets
    except ImportError as error:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "the WebSocket gateway needs the `websockets` package; "
            "install pydsh[ws]"
        ) from error

    gateway = Gateway(ctx, options, **gateway_options)
    server = await websockets.serve(gateway.handle, host, port)
    logger.info("gateway: listening on ws://%s:%s (no authentication)", host, port)
    return gateway, server


__all__ = [
    "Gateway",
    "serve",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_MAX_CONNECTIONS",
    "REFUSED_REASON",
    "MAX_FRAME_BYTES",
    "FrameTooLarge",
    "connection_io",
]
