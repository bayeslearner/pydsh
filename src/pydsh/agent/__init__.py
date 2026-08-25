"""The agent seam — the loop that drives a session's conversation.

Mount the registry, then the loop, on a context that already has
``ctx.sessions`` and ``ctx.llm``::

    await root.plugin(AgentRegistry)
    await root.plugin(AgentLoop)

    session = root.sessions.create()
    agent = root.agents.create_agent(session, AgentOptions(provider="p", model="m"))
    await agent.run("hello")

Callers go through ``ctx.agents`` and never name the loop, which is what makes
the loop replaceable: mount a different implementation, call
``ctx.agents.set_factory``, and nothing above has to change.
"""

from .agent import (
    AGENT_LOOP_SETTINGS,
    DEFAULT_MAX_PARALLEL_TOOL_CALLS,
    DEFAULT_MAX_STEPS,
    PRE_STEP,
    REQUEST,
    REQUEST_ERROR,
    SESSION_START,
    STATUS,
    Agent,
    AgentOptions,
)
from .assembler import BlockAssembler
from .inbox import NEXT_STEP, NEXT_TURN, SPLICE_EVENT, Inbox
from .registry import AgentLoop, AgentRegistry

__all__ = [
    "Agent",
    "AgentOptions",
    "AgentRegistry",
    "AgentLoop",
    "Inbox",
    "BlockAssembler",
    "NEXT_TURN",
    "NEXT_STEP",
    "SPLICE_EVENT",
    "DEFAULT_MAX_STEPS",
    "DEFAULT_MAX_PARALLEL_TOOL_CALLS",
    "PRE_STEP",
    "REQUEST",
    "REQUEST_ERROR",
    "STATUS",
    "SESSION_START",
    "AGENT_LOOP_SETTINGS",
]
