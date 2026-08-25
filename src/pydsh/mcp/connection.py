"""Supervised MCP connections — because the other process can die.

One supervisor per configured server. It connects, syncs the tool list, and
keeps both alive: a dropped connection is retried with bounded exponential
backoff, and after enough consecutive failures it gives up and **unregisters
the tools**. That last part matters. A tool the model is offered but which
cannot run costs a whole turn to discover, and the model has no way to tell
"this is broken" from "I used it wrong".

Syncs are serialised, because two generations swapping at once would each
dispose what the other just registered.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from plugkit import Service

from .bridge import (
    DEFAULT_TOOL_CALL_TIMEOUT,
    EMPTY_GENERATION,
    Generation,
    sync_tools,
)
from .client import McpClient, McpError, StdioTransport, StreamableHttpTransport

logger = logging.getLogger("pydsh.mcp")

#: The notification a server sends when its tool list changed.
TOOLS_CHANGED = "notifications/tools/list_changed"

#: Reconnect bounds, when config says nothing.
RECONNECT_DEFAULTS = {
    "enabled": True,
    "initial_delay": 0.5,
    "max_delay": 30.0,
    "max_attempts": 10,
}

#: How long a connection must stay up before its failures are forgiven. Without
#: this, a server that dies after an hour every time still exhausts the budget
#: — the failures are unrelated, and treating them as consecutive is wrong.
STABILITY_WINDOW_SECONDS = 60.0

#: The transports a server may be reached over.
TRANSPORT_KINDS = ("stdio", "http")


class McpConfigError(ValueError):
    """A server or reconnect configuration that cannot be served."""


def resolve_reconnect_policy(config: Optional[dict], path: str = "reconnect") -> dict:
    """Validate reconnect settings at mount, rejecting anything unusable."""
    config = config or {}
    for key in config:
        if key not in RECONNECT_DEFAULTS:
            raise McpConfigError(
                f"{path}.{key} is not a reconnect option "
                f"(options: {', '.join(sorted(RECONNECT_DEFAULTS))})"
            )
    policy = {**RECONNECT_DEFAULTS, **config}

    for key in ("initial_delay", "max_delay"):
        value = policy[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise McpConfigError(f"{path}.{key} must be a positive number")
    if policy["initial_delay"] > policy["max_delay"]:
        raise McpConfigError(
            f"{path}.initial_delay must not exceed {path}.max_delay"
        )
    attempts = policy["max_attempts"]
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise McpConfigError(f"{path}.max_attempts must be a positive integer")

    policy["enabled"] = bool(policy["enabled"])
    policy["initial_delay"] = float(policy["initial_delay"])
    policy["max_delay"] = float(policy["max_delay"])
    return policy


def resolve_server(name: str, config: Any) -> dict:
    """Validate one server's configuration at mount."""
    if not isinstance(config, dict):
        raise McpConfigError(f"mcp server {name!r} must be a mapping")

    kind = config.get("transport", "stdio")
    if kind not in TRANSPORT_KINDS:
        raise McpConfigError(
            f"mcp server {name!r} names transport {kind!r}; "
            f"transports are {', '.join(TRANSPORT_KINDS)}"
        )
    if kind == "stdio" and not config.get("command"):
        raise McpConfigError(f"mcp server {name!r} is stdio and needs a `command`")
    if kind == "http" and not config.get("url"):
        raise McpConfigError(f"mcp server {name!r} is http and needs a `url`")

    timeout = config.get("tool_call_timeout", DEFAULT_TOOL_CALL_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise McpConfigError(
            f"mcp server {name!r} tool_call_timeout must be a positive number"
        )

    return {
        "name": name,
        "transport": kind,
        "command": config.get("command"),
        "args": list(config.get("args") or ()),
        "env": dict(config.get("env") or {}),
        "cwd": config.get("cwd"),
        "url": config.get("url"),
        "headers": dict(config.get("headers") or {}),
        "tool_call_timeout": float(timeout),
        "fail_on_startup_error": bool(config.get("fail_on_startup_error", False)),
        "reconnect": resolve_reconnect_policy(
            config.get("reconnect"), f"mcp server {name!r} reconnect"
        ),
    }


def build_client(server: dict) -> McpClient:
    """The client for one configured server."""
    if server["transport"] == "stdio":
        transport: Any = StdioTransport(
            command=server["command"],
            args=server["args"],
            env=server["env"],
            cwd=server["cwd"],
        )
    else:
        transport = StreamableHttpTransport(server["url"], server["headers"])
    return McpClient(transport)


class Connection:
    """One supervised connection to one server."""

    def __init__(self, ctx: Any, server: dict, make_client=build_client) -> None:
        self.ctx = ctx
        self.server = server
        self._make_client = make_client
        self.client: Optional[McpClient] = None
        self.generation: Generation = EMPTY_GENERATION
        self.attempts = 0
        self.gave_up = False
        self._disposed = False
        self._connected_at: Optional[float] = None
        self._supervisor: Optional[asyncio.Task] = None
        # One chain, so two syncs cannot interleave their swaps.
        self._sync_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self.server["name"]

    async def start(self) -> None:
        """Connect once, then supervise. Raises only if configured to."""
        try:
            await self._connect_and_sync()
        except Exception as error:  # noqa: BLE001
            logger.warning("mcp(%s): first connection failed: %s", self.name, error)
            if self.server["fail_on_startup_error"]:
                raise
            self._schedule_retry()

    async def _connect_and_sync(self) -> None:
        client = self._make_client(self.server)
        client.on_notification(TOOLS_CHANGED, self._on_tools_changed)
        await client.connect()
        self.client = client
        self._connected_at = time.monotonic()
        self.attempts = 0
        await self.sync()

    async def sync(self) -> Generation:
        """Re-read the server's tools. Serialised against every other sync."""
        async with self._sync_lock:
            if self._disposed or self.client is None:
                return self.generation
            self.generation = await sync_tools(
                self.client,
                self.ctx,
                self.name,
                self.generation,
                self.server["tool_call_timeout"],
                "throw" if self.server["fail_on_startup_error"] else "contain",
            )
            return self.generation

    async def _on_tools_changed(self, _params: dict) -> None:
        try:
            await self.sync()
        except Exception as error:  # noqa: BLE001 - a notification is not a caller
            logger.warning("mcp(%s): re-sync after list_changed failed: %s", self.name, error)

    # -- supervision -------------------------------------------------------- #
    def _forgiven(self) -> bool:
        """Did this connection stay up long enough to forget its failures?"""
        return (
            self._connected_at is not None
            and time.monotonic() - self._connected_at >= STABILITY_WINDOW_SECONDS
        )

    def backoff(self) -> float:
        """The delay before the next attempt, exponential between the bounds."""
        policy = self.server["reconnect"]
        delay = policy["initial_delay"] * (2 ** max(0, self.attempts - 1))
        return min(delay, policy["max_delay"])

    def _schedule_retry(self) -> None:
        policy = self.server["reconnect"]
        if self._disposed or not policy["enabled"]:
            return
        if self._forgiven():
            # The failures were an hour apart. Counting them as consecutive
            # would exhaust the budget on a server that is mostly fine.
            self.attempts = 0
            self._connected_at = None
        self.attempts += 1
        if self.attempts > policy["max_attempts"]:
            asyncio.ensure_future(self._give_up())
            return
        self._supervisor = asyncio.ensure_future(self._retry_after(self.backoff()))

    async def _retry_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self._disposed:
            return
        try:
            await self._connect_and_sync()
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "mcp(%s): reconnect attempt %d failed: %s", self.name, self.attempts, error
            )
            self._schedule_retry()

    async def _give_up(self) -> None:
        """Stop trying, and take the tools away.

        Leaving them registered would offer the model something that cannot
        run — a whole turn spent discovering what this already knows.
        """
        self.gave_up = True
        logger.error(
            "mcp(%s): giving up after %d consecutive failures; its tools are "
            "no longer offered",
            self.name,
            self.attempts - 1,
        )
        async with self._sync_lock:
            self.generation.dispose()
            self.generation = Generation()

    async def dispose(self) -> None:
        """Stop supervising, close the client, unregister the tools."""
        if self._disposed:
            return
        self._disposed = True
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None:
            supervisor.cancel()
            try:
                await supervisor
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.close("the MCP connection was disposed")
            except Exception as error:  # noqa: BLE001
                logger.warning("mcp(%s): closing the client failed: %s", self.name, error)
        self.generation.dispose()
        self.generation = Generation()


class McpClientPlugin(Service):
    """Provides ``ctx.mcp`` — one supervised connection per configured server."""

    provide = "mcp"
    inject = ["tools"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        # Validated at mount, so an unusable server is a startup failure with
        # the field named rather than a mystery at the first tool call.
        self.servers = {
            name: resolve_server(name, server)
            for name, server in (config.get("servers") or {}).items()
        }
        self.connections: dict[str, Connection] = {}
        self._make_client = config.get("make_client", build_client)
        ctx.effect(lambda: lambda: asyncio.ensure_future(self.shutdown()))

    async def start(self) -> None:
        """Connect every configured server. Idempotent."""
        for name, server in self.servers.items():
            if name in self.connections:
                continue
            connection = Connection(self.ctx, server, self._make_client)
            self.connections[name] = connection
            await connection.start()

    def tools_of(self, name: str) -> list[str]:
        """The public names this server currently contributes."""
        connection = self.connections.get(name)
        return sorted(connection.generation.tools) if connection else []

    async def shutdown(self) -> None:
        """Dispose every connection. Idempotent."""
        connections, self.connections = self.connections, {}
        for connection in connections.values():
            try:
                await connection.dispose()
            except Exception as error:  # noqa: BLE001
                logger.warning("mcp: disposing a connection failed: %s", error)


__all__ = [
    "McpClientPlugin",
    "Connection",
    "McpConfigError",
    "resolve_reconnect_policy",
    "resolve_server",
    "build_client",
    "TOOLS_CHANGED",
    "RECONNECT_DEFAULTS",
    "STABILITY_WINDOW_SECONDS",
    "TRANSPORT_KINDS",
]
