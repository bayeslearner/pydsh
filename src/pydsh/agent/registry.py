"""The loop is a plugin — so it can be replaced without touching a caller.

Two services, deliberately split:

- :class:`AgentRegistry` provides ``ctx.agents`` and holds a *factory*. It is
  the only thing callers know about.
- :class:`AgentLoop` provides ``ctx.agent_loop``, implements the default loop,
  and registers itself as that factory on construction.

The indirection is the whole point. Swapping in a different loop is mounting
another implementation that calls ``ctx.agents.set_factory`` — every caller
still goes through ``ctx.agents.create_agent`` and never learns which one
answered.

The loop also owns a **teardown signal**. Unmounting the plugin aborts it,
which ends every agent whose lifetime was fused with it — otherwise agents
would keep running against a context that no longer has a loop.
"""

from __future__ import annotations

from typing import Any, Optional

from plugkit import Service

from ..cancel import CancelSignal
from .agent import Agent, AgentOptions


class AgentRegistry(Service):
    """``ctx.agents`` — who builds agents, held at one remove."""

    provide = "agents"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._factory: Optional[Any] = None

    def set_factory(self, factory: Any) -> None:
        """Register the loop implementation. The most recent one wins."""
        self._factory = factory

    def clear_factory(self, factory: Any) -> None:
        """Withdraw a factory on unmount, if it is still the registered one.

        Guarded by identity so a loop unloading *after* another has taken over
        does not withdraw its successor's registration — the same reason
        plugkit's points read the newest registration rather than restoring a
        saved value.
        """
        if self._factory is factory:
            self._factory = None

    def has_factory(self) -> bool:
        """Whether any loop is mounted."""
        return self._factory is not None

    def create_agent(
        self, session: Any, options: Optional[AgentOptions] = None, **kwargs: Any
    ) -> Agent:
        """Build an agent for a session through the mounted loop."""
        if self._factory is None:
            raise RuntimeError(
                "no agent factory is registered: mount pydsh.agent.AgentLoop, "
                "or register your own with ctx.agents.set_factory(...)"
            )
        return self._factory.create_agent(session, options, **kwargs)


class AgentLoop(Service):
    """``ctx.agent_loop`` — the default loop, and the registry's factory."""

    provide = "agent_loop"
    #: What the loop cannot run without. plugkit gates activation on these, so
    #: mounting the loop before the registry (or without an LLM seam) leaves it
    #: PENDING rather than half-working. ``tools`` is deliberately absent: it is
    #: optional, and `inject` has no optional form — see the note in
    #: :class:`~pydsh.agent.agent.Agent`.
    inject = ["agents", "sessions", "llm"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        # Held rather than read back through `ctx.agents` at teardown time: an
        # unloading fiber cannot resolve its injected services, so reaching for
        # the registry during disposal is exactly when it is unavailable.
        self._registry = ctx.agents
        self._agents: dict[str, Agent] = {}
        # Aborted when this plugin is unloaded. Every agent created here fuses
        # it into its lifetime, so unmounting the loop ends them rather than
        # leaving them running against a context that no longer has one.
        self._teardown = CancelSignal()
        ctx.effect(lambda: self._shutdown)
        self._registry.set_factory(self)

    def _shutdown(self) -> None:
        """Unmount: withdraw the factory and end every agent this loop created.

        Withdrawing matters as much as ending the agents. A registry still
        pointing at an unloaded loop would keep handing out agents built by it
        — working objects on a torn-down plugin, which is worse than the clear
        "no agent factory is registered" a caller gets instead.
        """
        self._teardown.abort("the agent loop was unmounted")
        self._registry.clear_factory(self)
        for agent in list(self._agents.values()):
            agent.dispose()
        self._agents.clear()

    def create_agent(
        self,
        session: Any,
        options: Optional[AgentOptions] = None,
        source: str = "startup",
        signal: Optional[CancelSignal] = None,
    ) -> Agent:
        """Build an agent bound to a session, and remember it."""
        lifetime = CancelSignal.any([signal, self._teardown])
        agent = Agent(self.ctx, session, options or AgentOptions(), source, lifetime)
        self._agents[agent.id] = agent
        return agent

    def get(self, session_id: str) -> Optional[Agent]:
        """The agent for a session, if this loop created one."""
        return self._agents.get(session_id)

    def roots(self) -> list[Agent]:
        """Every root agent.

        There is no sub-agent nesting yet, so every agent this loop created is
        a root. When lineage arrives this filters; the name is already right.
        """
        return list(self._agents.values())

    async def resume(
        self,
        session_id: str,
        options: Optional[AgentOptions] = None,
        signal: Optional[CancelSignal] = None,
    ) -> Agent:
        """Rebuild a persisted session and put an agent back on it."""
        session = await self.ctx.sessions.resume(session_id)
        return self.create_agent(session, options, source="resume", signal=signal)


__all__ = ["AgentRegistry", "AgentLoop"]
