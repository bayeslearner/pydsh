"""``subagent`` — a child agent on a standalone prompt.

The child gets a fresh session and sees nothing of the parent's conversation.
That is what makes the prompt have to be standalone, and it is the feature: a
subagent that inherited the history would inherit its size and its confusions
along with it.

Two things the reference gets wrong, and this does not.

**Depth is counted along the chain.** The reference increments one integer per
plugin instance, so five subagents started in parallel from a single turn each
add one and the fifth is refused for nesting five deep that never happened.
Worse, the failure only appears under parallel tool calls — exactly when it is
hardest to attribute. Here the depth is read from the calling agent, so
siblings are all at the same depth and only real nesting counts.

**The child dies with the parent's turn.** The reference never passes the
caller's signal down, so cancelling the parent leaves the child running to
completion, spending money on an answer that will be returned into a turn that
has already ended.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from plugkit import Service

from ..bounded.omitted import describe_omitted
from ..bounded.retention import TextRetainer
from ..message import as_text, decode_payload
from ..cancel import CancelSignal
from .agent import AgentOptions

#: How deep a chain of subagents may go. Three is enough for a real
#: decomposition and shallow enough that a runaway is caught early.
DEFAULT_MAX_DEPTH = 3

#: Bytes of the child's answer returned to the parent. The child's whole
#: transcript is not a tool result; it is the *answer* that is.
DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024

DEFAULT_TOOL_NAME = "subagent"

DEFAULT_DESCRIPTION = (
    "Delegate an independent task to a subagent. The prompt must stand alone: "
    "the subagent cannot see this conversation, so include everything it needs."
)

SUBAGENT_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": (
                "The complete, self-contained task for the subagent. It sees "
                "nothing of this conversation."
            ),
        }
    },
    "required": ["prompt"],
}

#: Where a chain's depth is recorded — on the agent, so it travels with the
#: branch rather than living in a counter shared by every branch at once.
DEPTH_ATTR = "_subagent_depth"


def branch_depth(agent: Any) -> int:
    """How deep the chain that reached this agent is. A root is zero."""
    return int(getattr(agent, DEPTH_ATTR, 0) or 0)


class _Tool:
    def __init__(self, name, description, parameters, execute) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.execute = execute


class SubagentTool(Service):
    """Registers the ``subagent`` tool."""

    provide = "subagent_tool"
    inject = ["tools"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        self._root = getattr(ctx, "root", ctx)
        self.provider = config.get("provider", "")
        self.model = config.get("model", "")
        self.system = config.get("system", "")
        self.max_depth = int(config.get("max_depth", DEFAULT_MAX_DEPTH))
        self._max_output = int(
            config.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)
        )

        dispose = ctx.tools.register(
            _Tool(
                config.get("tool_name", DEFAULT_TOOL_NAME),
                config.get("description", DEFAULT_DESCRIPTION),
                SUBAGENT_SCHEMA,
                self._run,
            )
        )
        ctx.effect(lambda: dispose)

    async def _run(self, arguments: dict, execution: Any = None) -> str:
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return "Error: subagent needs a non-empty, self-contained prompt."
        if not self.provider or not self.model:
            missing = " and ".join(
                name
                for name, value in (("provider", self.provider), ("model", self.model))
                if not value
            )
            return f"Error: the subagent tool has no {missing} configured."

        loop = getattr(self._root, "agent_loop", None)
        sessions = getattr(self._root, "sessions", None)
        if loop is None or sessions is None:
            return (
                "Error: subagent needs ctx.agent_loop and ctx.sessions; one of "
                "them is not mounted."
            )

        caller = getattr(execution, "caller", None)
        depth = branch_depth(caller) + 1
        if depth > self.max_depth:
            return (
                f"Error: subagent depth {depth} exceeds the limit of "
                f"{self.max_depth}; this task must be done without delegating."
            )

        return await self._delegate(loop, sessions, caller, prompt, depth)

    async def _delegate(
        self, loop: Any, sessions: Any, caller: Any, prompt: str, depth: int
    ) -> str:
        # The parent's id is in the name so a scratch session found in a log
        # directory can be traced back to the conversation that spawned it.
        parent = getattr(caller, "id", None) or "root"
        session_id = f"subagent-{parent}-{depth}-{uuid.uuid4().hex[:8]}"
        session = sessions.create(session_id)
        child: Optional[Any] = None
        try:
            child = loop.create_agent(
                session,
                AgentOptions(
                    provider=self.provider, model=self.model, system=self.system
                ),
                source="subagent",
                # The parent's *activity* signal, not its lifetime: the child
                # exists to serve this turn, and when the turn stops so does it.
                signal=_activity_of(caller),
            )
            setattr(child, DEPTH_ATTR, depth)
            await child.run(prompt)
            await child.when_idle()
            return self._answer(session)
        except Exception as exc:  # noqa: BLE001 - a child's failure is a result
            return f"Error: the subagent failed: {type(exc).__name__}: {exc}"
        finally:
            if child is not None:
                child.dispose()
            # On every path, including the failing one. A scratch session left
            # in the store is a conversation nobody will ever read again, held
            # for as long as the process runs.
            sessions.remove(session_id)

    def _answer(self, session: Any) -> str:
        """The child's assistant text, bounded and honest about the bound."""
        retainer = TextRetainer.head(self._max_output)
        wrote = False
        for event in session.events:
            if event.type != "assistant/message":
                continue
            data = event.data if isinstance(event.data, dict) else {}
            text = as_text(getattr(_decoded(data.get("message")), "content", ()) or ())
            if text:
                retainer.push(text if not wrote else "\n" + text)
                wrote = True

        result = retainer.finish()
        if not result["text"]:
            return "(the subagent produced no text)"
        if not result["truncated"]:
            return result["text"]
        notice = describe_omitted(result["omitted_bytes"], "bytes")
        return f"{result['text']}\n\n[{notice}]"


def _decoded(payload: Any) -> Any:
    if payload is None:
        return None
    try:
        return decode_payload(payload)
    except Exception:  # noqa: BLE001 - an unreadable message contributes nothing
        return None


def _activity_of(caller: Any) -> Optional[CancelSignal]:
    """The caller's turn signal, when there is a caller with one."""
    return getattr(caller, "activity", None)


__all__ = [
    "SubagentTool",
    "branch_depth",
    "SUBAGENT_SCHEMA",
    "DEPTH_ATTR",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_TOOL_NAME",
    "DEFAULT_DESCRIPTION",
]
