"""The runtime server — a booted context behind a JSON-RPC transport.

The same surface as the in-process `Harness`, one process away. A client sends
`session/run`; the server delivers the message, forwards each session event as
it happens, and answers when the agent goes idle.

One deliberate omission. The reference's `initialize` mounts a specific
vendor's adapter when the requested provider is not registered and happens to
be named after that vendor. This does not: a general core that silently mounts
a vendor's adapter has named a vendor, and it does so at exactly the moment the
caller should have been told the provider is not configured.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from ..boot.harness import final_response
from ..message import (
    MessageSource,
    TextBlock,
    ToolCallBlock,
    create_user_message,
    encode_payload,
)
from .protocol import METHOD_NOT_FOUND, JsonRpcTransport, MethodNotFound

logger = logging.getLogger("pydsh.runtime")

#: What this runtime calls itself on the wire.
SERVER_NAME = "pydsh-runtime"

#: The protocol this server speaks. Bumped when a frame's shape changes.
PROTOCOL_VERSION = "1"

#: The methods it answers.
METHODS = ("initialize", "session/prompt", "session/run", "session/list", "shutdown")

#: The notifications it sends.
EVENT_NOTIFICATION = "session.event"
STATUS_NOTIFICATION = "session.status"


def blocks_from_wire(content: Any) -> list:
    """Content blocks as they arrive on the wire.

    An unrecognised block is passed through unchanged rather than dropped —
    this layer is a translation, and silently losing part of a message is the
    one thing a translation must not do.
    """
    if isinstance(content, str):
        return [TextBlock(content)]
    if not isinstance(content, list):
        raise TypeError("content must be a string or a list of blocks")
    blocks: list = []
    for raw in content:
        if not isinstance(raw, dict):
            raise TypeError("each content block must be an object")
        kind = raw.get("type")
        if kind == "text":
            blocks.append(TextBlock(str(raw.get("text", ""))))
        elif kind == "tool-call":
            blocks.append(
                ToolCallBlock(
                    id=str(raw.get("tool_call_id") or raw.get("id") or ""),
                    name=str(raw.get("name", "")),
                    arguments=raw.get("arguments") or "",
                )
            )
        else:
            blocks.append(raw)
    return blocks


class RuntimeServer:
    """Serves one booted context over one transport."""

    def __init__(
        self, ctx: Any, transport: JsonRpcTransport, options: Any = None
    ) -> None:
        self.ctx = ctx
        self.transport = transport
        self.options = options
        self.route: dict = {}
        self._agents: dict = {}
        self._releases: list = []
        self._shutdown = False

        transport.on_request(self.handle)
        self._subscribe()

    # -- forwarding -------------------------------------------------------- #
    def _subscribe(self) -> None:
        for event, forward in (
            ("session/event", self._on_session_event),
            ("agent/status", self._on_agent_status),
        ):
            release = self.ctx.on(event, forward)
            if release is not None:
                self._releases.append(release)

    def _on_session_event(self, session: Any, event: Any) -> None:
        """Forward one append, **if this connection is in that conversation**.

        `session/event` fires for every session in the context, and a gateway
        runs one server per client over *one* shared context — so forwarding
        everything sends each client every other client's conversation. The
        filter is which sessions this connection has actually touched, not
        which it created: two clients naming the same session are genuinely in
        it together and both should see it.

        Contained — see `notify` (I3).
        """
        session_id = getattr(session, "id", None)
        if session_id not in self._agents:
            return
        self.transport.notify(
            EVENT_NOTIFICATION,
            {
                "session_id": getattr(session, "id", None),
                "seq": getattr(event, "seq", None),
                "type": getattr(event, "type", None),
                "time": getattr(event, "time", None),
                "data": _wire_safe(getattr(event, "data", None)),
            },
        )

    def _on_agent_status(self, payload: Any) -> None:
        agent = (payload or {}).get("agent") if isinstance(payload, dict) else None
        if getattr(agent, "id", None) not in self._agents:
            return  # another connection's agent
        self.transport.notify(
            STATUS_NOTIFICATION,
            {
                "session_id": getattr(agent, "id", None),
                "status": (payload or {}).get("status") if isinstance(payload, dict) else None,
            },
        )

    # -- dispatch ---------------------------------------------------------- #
    async def handle(self, method: str, params: dict) -> Any:
        """Answer one request. An unknown method is an answer, not a crash."""
        if method == "initialize":
            return await self.initialize(params)
        if method == "session/prompt":
            return await self.prompt(params)
        if method == "session/run":
            return await self.run(params)
        if method == "session/list":
            return self.list_sessions()
        if method == "shutdown":
            return await self.shutdown()
        raise MethodNotFound(
            f"no method {method!r}; this runtime serves {', '.join(METHODS)}"
        )

    # -- methods ----------------------------------------------------------- #
    async def initialize(self, params: dict) -> dict:
        """Handshake, and fix the route this connection will use."""
        provider = params.get("provider") or ""
        model = params.get("model") or ""
        if provider and not self._can_route(provider):
            routable = ", ".join(self._routes()) or "none"
            raise ValueError(
                f"this runtime cannot route provider {provider!r} "
                f"(it can route: {routable})"
            )
        self.route = {
            "provider": provider,
            "model": model,
            "max_tokens": params.get("max_tokens"),
        }
        return {
            "server": {"name": SERVER_NAME, "version": _version()},
            "protocol_version": PROTOCOL_VERSION,
            "providers": self._routes(),
            "methods": list(METHODS),
        }

    async def prompt(self, params: dict) -> dict:
        """Deliver a message and return at once, without waiting for the turn."""
        agent, message = await self._deliver(params)
        agent.insert(message)
        return {"session_id": agent.session.id, "message_id": message.id}

    async def run(self, params: dict) -> dict:
        """Deliver a message and wait for the turn to finish."""
        agent, message = await self._deliver(params)
        agent.insert(message)
        await agent.when_idle()
        session = agent.session
        return {
            "session_id": session.id,
            "message_id": message.id,
            "final_response": final_response(session),
            "event_count": len(session.events),
        }

    def list_sessions(self) -> dict:
        sessions = getattr(self.ctx, "sessions", None)
        return {"sessions": [s.id for s in sessions.list()] if sessions else []}

    async def shutdown(self) -> dict:
        """Unsubscribe, stop every agent, and say so. Idempotent."""
        if self._shutdown:
            return {"ok": True, "already": True}
        self._shutdown = True
        for release in self._releases:
            try:
                release()
            except Exception:  # noqa: BLE001 - teardown is best-effort
                pass
        self._releases.clear()
        for agent in self._agents.values():
            try:
                agent.cancel("the runtime is shutting down")
                agent.dispose()
            except Exception:  # noqa: BLE001
                pass
        self._agents.clear()
        return {"ok": True}

    # -- internals --------------------------------------------------------- #
    async def _deliver(self, params: dict) -> tuple:
        if self._shutdown:
            raise RuntimeError("this runtime has shut down")
        session_id = params.get("session_id")
        if not session_id:
            raise ValueError("session_id is required")
        blocks = blocks_from_wire(params.get("content", params.get("text", [])))
        if not blocks:
            raise ValueError("a prompt needs content")
        agent = self._agent_for(str(session_id))
        return agent, create_user_message(blocks, MessageSource("user"))

    def _agent_for(self, session_id: str) -> Any:
        existing = self._agents.get(session_id)
        if existing is not None:
            return existing
        agents = getattr(self.ctx, "agents", None)
        sessions = getattr(self.ctx, "sessions", None)
        if agents is None or sessions is None:
            raise RuntimeError(
                "this runtime's context has no agent registry or session store"
            )
        session = sessions.get(session_id) or sessions.create(session_id)
        agent = agents.create_agent(session, self._options())
        self._agents[session_id] = agent
        return agent

    def _options(self) -> Any:
        from ..agent import AgentOptions

        if self.options is not None and not self.route:
            return self.options
        base = self.options
        return AgentOptions(
            provider=self.route.get("provider") or getattr(base, "provider", ""),
            model=self.route.get("model") or getattr(base, "model", ""),
            system=getattr(base, "system", "") if base else "",
            max_tokens=self.route.get("max_tokens")
            or (getattr(base, "max_tokens", None) if base else None),
        )

    def _routes(self) -> list:
        llm = getattr(self.ctx, "llm", None)
        if llm is None:
            return []
        try:
            return [info.id for info in llm.list_providers()]
        except Exception:  # noqa: BLE001
            return []

    def _can_route(self, provider: str) -> bool:
        return provider in self._routes()


def _wire_safe(value: Any) -> Any:
    """A payload a client can parse, whatever the event carried."""
    try:
        return encode_payload(value)
    except Exception:  # noqa: BLE001 - a describable value beats a dropped one
        return repr(value)


def _version() -> str:
    from .. import __version__

    return __version__


async def serve(ctx: Any, transport: JsonRpcTransport, options: Any = None) -> RuntimeServer:
    """Attach a server to a transport and start reading."""
    server = RuntimeServer(ctx, transport, options)
    transport.start()
    return server


__all__ = [
    "RuntimeServer",
    "MethodNotFound",
    "serve",
    "blocks_from_wire",
    "SERVER_NAME",
    "PROTOCOL_VERSION",
    "METHODS",
    "EVENT_NOTIFICATION",
    "STATUS_NOTIFICATION",
    "METHOD_NOT_FOUND",
]
