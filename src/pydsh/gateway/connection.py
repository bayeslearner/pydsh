"""Making a socket look like a reader and a writer.

That is the whole module, and it is the reason the gateway is small. Sprint
21's `JsonRpcTransport` takes an injected reader and writer — a seam built for
testing — and a WebSocket connection is exactly that pair. The reference writes
a second transport class instead, with its own copy of frame dispatch; frame
dispatch is the part most likely to need a fix, and two copies means every fix
has to be remembered twice.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from ..runtime.protocol import Reader, Writer

logger = logging.getLogger("pydsh.gateway")

#: The largest frame accepted. A message bigger than this is refused rather
#: than buffered — the alternative is letting one client decide how much memory
#: this process uses.
MAX_FRAME_BYTES = 1 << 20


class FrameTooLarge(Exception):
    """A client sent more in one frame than this gateway accepts."""


def connection_io(
    connection: Any, max_frame_bytes: int = MAX_FRAME_BYTES
) -> tuple[Reader, Writer]:
    """Adapt an object with ``recv``/``send`` into the transport's seam.

    :param connection: anything with an awaitable ``recv()`` and ``send(str)``
        — a `websockets` connection, or a fake in a test.
    :returns: ``(reader, writer)`` for :class:`~pydsh.runtime.JsonRpcTransport`.
    """
    closed = False

    async def read() -> Optional[str]:
        nonlocal closed
        if closed:
            return None
        try:
            frame = await connection.recv()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - any close looks the same from here
            closed = True
            return None
        if frame is None:
            closed = True
            return None
        if isinstance(frame, (bytes, bytearray)):
            frame = frame.decode("utf-8", "replace")
        if len(frame.encode("utf-8")) > max_frame_bytes:
            closed = True
            # Closed, not skipped: a client sending oversized frames will send
            # another, and skipping means reading them all forever.
            asyncio.ensure_future(_close(connection, "frame too large"))
            raise FrameTooLarge(
                f"a frame exceeded {max_frame_bytes} bytes; the connection was closed"
            )
        return frame

    def write(line: str) -> None:
        if closed:
            return
        result = connection.send(line)
        if hasattr(result, "__await__"):
            # Fire-and-forget: the writer is called from `notify`, which is
            # called from an observer, where raising is forbidden. A failed
            # send here means the client is gone, and there is nobody to tell.
            task = asyncio.ensure_future(_send_soft(result))
            _pending.add(task)
            task.add_done_callback(_pending.discard)

    return read, write


#: Held so a fire-and-forget send is not garbage-collected mid-flight.
_pending: set = set()


async def _send_soft(awaitable: Any) -> None:
    try:
        await awaitable
    except Exception as error:  # noqa: BLE001 - the client is gone
        logger.debug("gateway: a frame could not be sent: %s", error)


async def _close(connection: Any, reason: str) -> None:
    try:
        result = connection.close()
        if hasattr(result, "__await__"):
            await result
    except Exception as error:  # noqa: BLE001
        logger.debug("gateway: closing a connection failed: %s", error)


__all__ = ["connection_io", "FrameTooLarge", "MAX_FRAME_BYTES"]
