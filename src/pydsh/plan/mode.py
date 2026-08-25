"""``ctx.plan_mode`` — a recorded collaboration state, folded from the log.

While plan mode is in effect the deployment's guidance joins the system prompt,
and `exit_plan_mode` presents the finished plan for review. The state itself is
one thing: the last `plan/mode` event. There is no authoritative in-memory
copy, so a resumed or forked session is in plan mode because its log says so,
with nothing to rebuild and nothing to keep in sync.

The one piece of memory here is a **pending intent** — a flip requested while a
turn is running, held until the next turn boundary. Applying it immediately
would change the rules the model is working under, halfway through the work it
was asked to do. The intent is deliberately not persisted: a choice made
mid-turn and never applied is not a fact about the conversation, and reviving
one across a restart would flip the policy on a session the person has since
moved on from.
"""

from __future__ import annotations

import json
import weakref
from typing import Any, Optional

from plugkit import Service

from ..message import MessageSource, TextBlock, create_user_message
from ..operating.commands import CommandResult
from ..prompt.sections import PromptSection
from ..session.projection import ProjectionDefinition

#: The event that records a flip. Last one wins.
PLAN_EVENT = "plan/mode"

#: The tool a model calls to present its plan.
EXIT_PLAN_MODE = "exit_plan_mode"

#: The prompt section the guidance is carried in.
POLICY_SECTION = "plan:policy"

#: Where the section sits among the others.
POLICY_ORDER = 50

#: The key the plan projection owns.
PLAN_KEY = "plan"

#: What `/plan` accepts as "leave plan mode".
OFF = "off"

USAGE = "Usage: /plan [<message>|off]"

EXIT_DESCRIPTION = (
    "Use only in plan mode. Present your plan for the user's review and, on "
    "approval, leave plan mode. Send the COMPLETE plan as markdown, starting "
    "with a # heading that names it. The user may approve (carry out the plan "
    "from your next step) or keep planning — their feedback comes back in the "
    "tool result; revise and present again."
)

EXIT_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {
            "type": "string",
            "description": (
                "The complete plan, as markdown, starting with a # heading "
                "that names it."
            ),
        }
    },
    "required": ["plan"],
}


class PlanModeError(ValueError):
    """A plan-mode configuration or call that cannot stand."""


def fold_plan_mode(events: Any) -> bool:
    """Whether plan mode is recorded as in effect. Last `plan/mode` wins."""
    active = False
    for event in events:
        if event.type == PLAN_EVENT:
            data = event.data if isinstance(event.data, dict) else {}
            active = bool(data.get("active", False))
    return active


def has_open_turn(events: Any) -> bool:
    """Is a turn running? A `turn/start` with no `turn/end` after it."""
    open_ = False
    for event in events:
        if event.type == "turn/start":
            open_ = True
        elif event.type == "turn/end":
            open_ = False
    return open_


def last_turn_start_seq(events: Any) -> int:
    """The seq of the most recent ``turn/start``, or 0 before the first.

    This — not the turn *number* — is what a pending intent is stamped with.
    The number comes from the agent's in-memory counter, which can disagree
    with the log when a session was written by someone else or resumed onto a
    fresh agent; a sequence number is assigned by the log itself and only ever
    goes up, so "a later turn than the one I was queued in" is decidable
    without trusting anyone's count.
    """
    seq = 0
    for event in events:
        if event.type == "turn/start":
            seq = event.seq
    return seq


def _apply(state: dict, event: Any) -> dict:
    if event.type != PLAN_EVENT:
        return state
    data = event.data if isinstance(event.data, dict) else {}
    active = bool(data.get("active", False))
    # Identity is the change gate: returning an equal-but-new dict would mark
    # the cell changed on an event that changed nothing.
    return state if active == state["active"] else {"active": active}


#: The recorded state, and only that. A pending intent is not in the log — see
#: the module docstring — so a `pending` key here could never be true.
PLAN_PROJECTION = ProjectionDefinition(
    key=PLAN_KEY,
    init=lambda: {"active": False},
    apply=_apply,
    view=lambda state: {"active": state["active"]},
    state_version=1,
)


class _Tool:
    def __init__(self, name, description, parameters, execute) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.execute = execute


class PlanMode(Service):
    """Provides ``ctx.plan_mode``."""

    provide = "plan_mode"
    inject = ["tools"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self.section = _resolve_section(config)
        self._root = getattr(ctx, "root", ctx)
        # Weakly keyed: an intent is about a session, and a forgotten session
        # should not be kept alive by a choice nobody applied. Each entry is
        # ``{"active": bool, "turn": int}`` — the turn it was queued *in*, so
        # it can be told apart from the turn it should land in.
        self._pending: "weakref.WeakKeyDictionary[Any, dict]" = (
            weakref.WeakKeyDictionary()
        )

        ctx.on("agent/pre-step", self._on_pre_step)

        prompt = getattr(self._root, "system_prompt", None)
        if prompt is not None:
            release = prompt.section(
                PromptSection(
                    name=POLICY_SECTION, order=POLICY_ORDER, text=self._policy_text
                )
            )
            ctx.effect(lambda: release)

        projections = getattr(self._root, "session_projections", None)
        if projections is not None:
            release_projection = projections.register(PLAN_PROJECTION)
            ctx.effect(lambda: release_projection)

        commands = getattr(self._root, "commands", None)
        if commands is not None:
            release_command = commands.register(
                "plan", "Enter or leave plan mode", self._run_command
            )
            ctx.effect(lambda: release_command)

        dispose_tool = ctx.tools.register(
            _Tool(EXIT_PLAN_MODE, EXIT_DESCRIPTION, EXIT_SCHEMA, self._exit)
        )
        ctx.effect(lambda: dispose_tool)

    # -- reading and choosing ---------------------------------------------- #
    def get(self, agent: Any) -> dict:
        """The recorded state, plus a pending intent when one is held."""
        session = agent.session
        state = {"active": fold_plan_mode(session.events)}
        pending = self._pending.get(session)
        if pending is not None:
            state["pending"] = pending["active"]
        return state

    def set(self, agent: Any, active: bool) -> str:
        """Choose whether plan mode is in effect.

        :returns: ``committed`` (recorded now), ``queued`` (held for the next
            turn boundary), ``cancelled`` (an earlier pending intent dropped),
            or ``noop``.
        """
        session = agent.session
        recorded = fold_plan_mode(session.events)
        pending = self._pending.get(session)
        target = pending["active"] if pending is not None else recorded
        if active == target:
            return "noop"

        if has_open_turn(session.events):
            if active == recorded:
                # It restores what is already recorded, so there is nothing to
                # apply. Storing the intent anyway — as the reference does —
                # queues a write that does nothing while reporting "cancelled",
                # which is two different claims about one action.
                self._pending.pop(session, None)
                return "cancelled"
            self._pending[session] = {
                "active": active,
                "turn_seq": last_turn_start_seq(session.events),
            }
            return "queued"

        session.append(PLAN_EVENT, {"active": active})
        self._pending.pop(session, None)
        return "committed"

    # -- the boundary ------------------------------------------------------- #
    def apply_pending(self, session: Any) -> bool:
        """Record a held intent, if this is a *later* turn than it was queued in.

        The turn check is the whole of I2. Without it, a flip requested during
        step one of turn five is applied inside step one of turn five — the
        turn it was explicitly queued out of — and the queueing bought nothing.
        """
        pending = self._pending.get(session)
        if pending is None or last_turn_start_seq(session.events) <= pending["turn_seq"]:
            return False
        self._pending.pop(session, None)
        if pending["active"] == fold_plan_mode(session.events):
            return False  # the log caught up on its own
        session.append(PLAN_EVENT, {"active": pending["active"]})
        return True

    async def _on_pre_step(self, payload: dict, next_: Any) -> Any:
        """Apply a held intent before the turn it belongs to assembles."""
        decision = await next_()
        if not isinstance(decision, dict) or decision.get("kind") != "enter":
            return decision
        agent = payload.get("agent")
        session = getattr(agent, "session", None)
        if session is None or payload.get("step") != 1:
            # Step one only. Applying mid-turn is exactly what the pending
            # mechanism exists to avoid (I2).
            return decision
        try:
            self.apply_pending(session)
        except Exception:  # noqa: BLE001 - a policy flip must not block a step
            pass
        return decision

    # -- the prompt section ------------------------------------------------- #
    def _policy_text(self, context: dict) -> str:
        """The guidance, if plan mode is **recorded** as on.

        The recorded state, never the pending one — which is the whole of I2.
        A pending intent is held precisely because a turn is running; letting
        it render here would apply the new policy at step two of a turn that
        began under the old one, while still reporting the flip as queued.
        By the time it should take effect, `apply_pending` has recorded it and
        the fold says so.
        """
        agent = context.get("agent")
        if agent is None:
            return ""
        return self.section if fold_plan_mode(agent.session.events) else ""

    # -- /plan -------------------------------------------------------------- #
    def _run_command(self, invocation: Any) -> CommandResult:
        agent = invocation.agent
        if agent is None:
            return CommandResult.error(
                f"/plan needs a session to act on. {USAGE}"
            )
        message = (invocation.raw_input or "").strip()

        if message.lower() == OFF:
            outcome = self.set(agent, False)
            if outcome == "committed":
                return CommandResult.success("Plan mode off.")
            if outcome == "queued":
                return CommandResult.success(
                    "Leaving plan mode (applies from the next step)."
                )
            if outcome == "cancelled":
                return CommandResult.success("Plan mode entry cancelled.")
            return CommandResult.success("Plan mode is already inactive.")

        outcome = self.set(agent, True)
        if message:
            # Delivered as an ordinary user message: the person typed it, and
            # /plan is how it arrived, not what it is.
            agent.insert(
                create_user_message([TextBlock(message)], MessageSource("user"))
            )
        if outcome == "committed":
            return CommandResult.success("Plan mode on. Use /plan off to leave.")
        if outcome == "noop":
            return CommandResult.success("Plan mode is already on.")
        return CommandResult.success(
            "Entering plan mode (applies from the next step). Use /plan off to leave."
        )

    # -- exit_plan_mode ----------------------------------------------------- #
    async def _exit(self, arguments: dict, execution: Any = None) -> str:
        agent = getattr(execution, "caller", None)
        session = getattr(agent, "session", None)
        if session is None:
            return (
                f"Error: {EXIT_PLAN_MODE} needs a calling agent — there is no "
                "session to leave plan mode in."
            )
        if not fold_plan_mode(session.events):
            return f"Error: {EXIT_PLAN_MODE} is only available in plan mode."

        plan = arguments.get("plan")
        if not _is_markdown_plan(plan):
            return (
                f"Error: {EXIT_PLAN_MODE} needs a plan in markdown, starting "
                "with a # heading that names it."
            )

        review = getattr(self._root, "user_questions", None)
        if review is None:
            # Named rather than silently approved. An approval nobody gave is
            # the one outcome this tool must never manufacture.
            return (
                "Error: no review channel (ctx.user_questions) is mounted, so "
                "the plan cannot be put to the user. Ask them to leave plan "
                "mode instead."
            )

        approved = await review.ask_approval(agent, plan)
        if not approved:
            return json.dumps({"approved": False})
        # Queued, not committed: the plan was approved *during* a turn, and the
        # description promises the model carries it out "from your next step".
        self._pending[session] = {
            "active": False,
            "turn_seq": last_turn_start_seq(session.events),
        }
        return json.dumps({"approved": True})


def _is_markdown_plan(plan: Any) -> bool:
    """A plan is markdown starting with a heading, and says something."""
    if not isinstance(plan, str):
        return False
    stripped = plan.strip()
    return stripped.startswith("# ") and len(stripped) > 2


def _resolve_section(config: Any) -> str:
    """The deployment's guidance, validated at construction (R1.1).

    Failing here rather than at first use means a misconfigured deployment
    cannot reach the state where plan mode is on and the model is told nothing
    about what plan mode means.
    """
    config = config or {}
    unknown = [key for key in config if key != "section"]
    if unknown:
        raise PlanModeError(
            f"plan mode config has unknown key(s) {', '.join(map(str, unknown))} "
            "— it takes only `section`"
        )
    section = config.get("section")
    if not isinstance(section, str):
        raise PlanModeError("plan mode needs a string `section`")
    if not section.strip():
        raise PlanModeError(
            "plan mode needs a non-empty `section`: with plan mode on and no "
            "guidance, the model is held to a policy it was never told"
        )
    return section


__all__ = [
    "PlanMode",
    "PlanModeError",
    "fold_plan_mode",
    "has_open_turn",
    "last_turn_start_seq",
    "PLAN_PROJECTION",
    "PLAN_EVENT",
    "PLAN_KEY",
    "EXIT_PLAN_MODE",
    "EXIT_SCHEMA",
    "EXIT_DESCRIPTION",
    "POLICY_SECTION",
    "POLICY_ORDER",
    "USAGE",
]
