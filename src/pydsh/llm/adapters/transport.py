"""The transport seam — what turns a request into a stream of SSE lines.

A seam rather than a hard-wired client, for two reasons. It is how every test
in this package feeds *real* SSE bytes without opening a socket, which is the
only honest way to test a truncated stream or a malformed frame. And it is how
a deployment that already has an HTTP client — with its proxy configuration,
its connection pool, its tracing — uses that one instead of a second.

The default implementation is httpx, imported lazily so the core's dependency
list stays at one entry. A consumer who wants the batteries installs
``pydsh[http]``; a consumer who brings a transport never does.
"""

from __future__ import annotations

import asyncio

from typing import Any, AsyncIterator, Callable, Optional

from ..errors import LlmError

#: ``(url, body, headers, signal) -> AsyncIterator[str]`` — one SSE line each.
#: The signal is part of the contract, not an afterthought: a cancelled turn
#: has to reach the socket, or the request runs to completion and is paid for
#: after nobody is waiting for it.
Transport = Callable[[str, dict, dict, Any], AsyncIterator[str]]

#: How long a streaming request may take in total. Generous — a long answer
#: with reasoning is genuinely slow — but not unbounded, because an endpoint
#: that accepts a connection and then says nothing would hang a turn forever.
DEFAULT_TIMEOUT_SECONDS = 300.0

#: Bytes of an error body quoted in the raised message. A whole error body from
#: an unknown endpoint is not something to write into a session log.
ERROR_BODY_CHARS = 200


class TransportUnavailable(LlmError):
    """No transport was given and the default one is not installed."""

    def __init__(self, message: str) -> None:
        super().__init__(message, "TRANSPORT_UNAVAILABLE")


async def httpx_transport(
    url: str,
    body: dict,
    headers: dict,
    signal: Any = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> AsyncIterator[str]:
    """Stream an SSE response with httpx. Requires the ``http`` extra."""
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise TransportUnavailable(
            "no transport was supplied and httpx is not installed; install "
            "pydsh[http] or pass transport= when registering the adapter"
        ) from exc

    async with httpx.AsyncClient() as client:
        async with client.stream(
            "POST", url, json=body, headers=headers, timeout=timeout
        ) as response:
            if response.status_code >= 400:
                detail = (await response.aread())[:ERROR_BODY_CHARS]
                raise LlmError(
                    f"HTTP {response.status_code}: {detail!r}",
                    f"HTTP_{response.status_code}",
                )
            async for line in response.aiter_lines():
                if aborted(signal):
                    # Leaving the `async with` closes the connection, which is
                    # what actually stops the endpoint generating. Checking
                    # only in the loop above this would keep the socket open
                    # and the meter running.
                    return
                yield line


def aborted(signal: Any) -> bool:
    """Has this call been cancelled? Tolerant of there being no signal."""
    return bool(getattr(signal, "aborted", False))


async def with_idle_timeout(
    lines: AsyncIterator[str], seconds: float
) -> AsyncIterator[str]:
    """Fail a stream that stalls between lines for longer than ``seconds``.

    An *idle* bound, not a total one: a long answer is allowed to take as long
    as it takes, but an endpoint that accepts the connection and then goes
    quiet would otherwise hold a turn open forever, looking exactly like a slow
    model.
    """
    iterator = lines.__aiter__()
    while True:
        try:
            line = await asyncio.wait_for(iterator.__anext__(), timeout=seconds)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError as exc:
            raise LlmError(
                f"the stream produced nothing for {seconds}s", "TIMEOUT"
            ) from exc
        yield line


def resolve_transport(transport: Optional[Transport]) -> Transport:
    """The transport to use: the given one, or the lazy httpx default."""
    return transport if transport is not None else httpx_transport


__all__ = [
    "Transport",
    "httpx_transport",
    "resolve_transport",
    "with_idle_timeout",
    "aborted",
    "TransportUnavailable",
    "DEFAULT_TIMEOUT_SECONDS",
    "ERROR_BODY_CHARS",
]
