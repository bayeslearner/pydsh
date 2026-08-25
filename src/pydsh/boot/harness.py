"""The SDK — the shortest path from nothing to a running turn.

::

    async with Harness(profile=my_profile) as harness:
        result = await harness.session().run("what changed today?")
        print(result.final_response)

Assembly is **lazy**, not done in the constructor. Mounting is async and can
fail; a constructor cannot await, and an object that half-built itself has no
way to say which half — the caller gets something that exists and does not
work.

The answer is read back **out of the log**, not from whatever the adapter
yielded. The log is what anyone reads afterwards, and if the returned string
and the transcript can disagree — because a plugin rewrote the message, or
compaction ran — the disagreement stays invisible until someone compares them,
which is usually during an incident.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..message import as_text, decode_payload
from .envfile import load_layered_env
from .home import resolve_home
from .profile import (
    ProfileError,
    core_profile,
    mount_profile,
    resolve_profile,
    unmount,
)

#: How a session is named when the caller does not name it.
SESSION_ID_PREFIX = "session-"


class HarnessError(RuntimeError):
    """The harness cannot do what was asked."""


@dataclass
class RunResult:
    """What one turn produced."""

    session_id: str
    #: The last assistant text on the session's surface, or ``""``.
    final_response: str
    events: list = field(default_factory=list)
    #: The live session, for a caller that wants more than the answer.
    session: Any = None


def final_response(session: Any) -> str:
    """The last assistant text in a session's log.

    Read from the log rather than tracked alongside it, so the answer a caller
    gets is the same string a reader of the transcript sees.
    """
    for event in reversed(list(session.events)):
        if event.type != "assistant/message":
            continue
        data = event.data if isinstance(event.data, dict) else {}
        try:
            message = decode_payload(data.get("message"))
        except Exception:  # noqa: BLE001 - an unreadable payload is not the answer
            continue
        text = as_text(getattr(message, "content", ()) or ())
        if text:
            return text
    return ""


class HarnessSession:
    """A handle on one conversation."""

    def __init__(self, harness: "Harness", session_id: str) -> None:
        self.harness = harness
        self.id = session_id

    async def run(self, text: str, options: Any = None) -> RunResult:
        """Deliver one message, wait for the turn, and read the answer back."""
        agent = await self.harness._agent_for(self.id, options)
        await agent.run(text)
        await agent.when_idle()
        session = agent.session
        return RunResult(
            session_id=self.id,
            final_response=final_response(session),
            events=list(session.events),
            session=session,
        )

    async def send(self, text: str, options: Any = None) -> None:
        """Deliver a message without waiting — for a caller driving its own loop."""
        agent = await self.harness._agent_for(self.id, options)
        from ..message import MessageSource, TextBlock, create_user_message

        agent.insert(create_user_message([TextBlock(text)], MessageSource("user")))


class Harness:
    """A whole assembled context, and the sessions on it."""

    def __init__(
        self,
        profile: Any = None,
        *,
        home: Optional[str] = None,
        cwd: Optional[str] = None,
        env: Optional[Any] = None,
        options: Any = None,
    ) -> None:
        self._profile = profile
        self.home = resolve_home(home)
        self._cwd = cwd
        self._inherited = env
        self.options = options
        self.ctx: Any = None
        self.env: dict = {}
        self._mounted: list = []
        self._agents: dict = {}
        self._closed = False

    # -- lifecycle --------------------------------------------------------- #
    async def start(self) -> Any:
        """Assemble the context. Idempotent."""
        if self.ctx is not None:
            return self.ctx
        if self._closed:
            raise HarnessError("this harness has been closed")

        from plugkit import Context

        # The environment first: the profile's configs interpolate against it.
        self.env = load_layered_env(self._cwd, self.home, self._inherited)
        profile = self._profile if self._profile is not None else core_profile()
        # Everything resolved before anything mounts (I5).
        entries = resolve_profile(profile, self.env)

        ctx = Context()
        # `mount_profile` unmounts whatever it managed before re-raising, so a
        # failed assembly leaves nothing running rather than a half-built
        # context that a caller can hold and use.
        self._mounted = await mount_profile(ctx, entries)
        self.ctx = ctx
        return ctx

    async def close(self) -> None:
        """Unmount everything. Idempotent, and safe after a failed start."""
        if self._closed:
            return
        self._closed = True
        self._agents.clear()
        self.ctx = None
        mounted, self._mounted = self._mounted, []
        await unmount(mounted)

    async def __aenter__(self) -> "Harness":
        await self.start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        # On every path, including the one where the turn raised (I6).
        await self.close()

    # -- sessions ---------------------------------------------------------- #
    def session(self, session_id: Optional[str] = None) -> HarnessSession:
        """A handle on a conversation, naming it if the caller did not."""
        return HarnessSession(self, session_id or f"{SESSION_ID_PREFIX}{uuid.uuid4().hex[:12]}")

    async def _agent_for(self, session_id: str, options: Any = None) -> Any:
        ctx = await self.start()
        existing = self._agents.get(session_id)
        if existing is not None:
            return existing

        agents = getattr(ctx, "agents", None)
        if agents is None:
            raise HarnessError(
                "this profile mounts no agent registry, so there is nothing to "
                "run a turn with"
            )
        sessions = getattr(ctx, "sessions", None)
        if sessions is None:
            raise HarnessError("this profile mounts no session store")

        session = sessions.get(session_id) or sessions.create(session_id)
        agent = agents.create_agent(session, options or self.options)
        self._agents[session_id] = agent
        return agent


__all__ = [
    "Harness",
    "HarnessSession",
    "HarnessError",
    "RunResult",
    "final_response",
    "SESSION_ID_PREFIX",
]
