"""``/goal`` — set, read, and steer the session's objective.

The parser owns exactly the words `/goal` defines — `clear`, `pause`,
`resume`, `edit <text>` — and treats everything else as an objective. That
asymmetry is deliberate: a person typing a goal should not have to escape it
because it happens to start with a word the command reserved, and the reserved
words are few enough to keep in one's head.

Nothing here exposes compare-and-set. `ctx.goals` reads the current goal and
derives the next revision itself; a person typing a sentence should never meet
a revision number.
"""

from __future__ import annotations

from typing import Any

from plugkit import Service

from ..operating.commands import CommandResult
from ..work.goal_fold import GoalError

USAGE = "Usage: /goal [<objective>|clear|edit <objective>|pause|resume]"

#: The words `/goal` owns. Everything else is an objective.
CONTROLS = ("clear", "pause", "resume")


def parse_goal_command(raw_input: str) -> dict:
    """What the person asked for."""
    text = (raw_input or "").strip()
    if not text:
        return {"kind": "show"}
    lowered = text.lower()
    if lowered in CONTROLS:
        return {"kind": lowered}
    if lowered == "edit":
        return {"kind": "invalid-edit"}
    if lowered.startswith("edit") and len(text) > 4 and text[4].isspace():
        objective = text[4:].strip()
        return {"kind": "edit", "objective": objective} if objective else {
            "kind": "invalid-edit"
        }
    return {"kind": "create", "objective": text}


def _commands_from(status: str) -> str:
    """What is worth doing next, given where the goal is."""
    if status == "active":
        return "/goal edit <objective>, /goal pause, /goal clear"
    if status == "paused":
        return "/goal edit <objective>, /goal resume, /goal clear"
    if status == "blocked":
        return "/goal edit <objective>, /goal clear"
    return "/goal <objective>"  # completed


def render_goal(title: str, goal: dict, rounds: int, max_rounds: int) -> CommandResult:
    return CommandResult.success(
        "\n".join(
            [
                title,
                f"Status: {goal['status']}",
                f"Objective: {goal['text']}",
                f"Rounds: {rounds}/{max_rounds}",
                "",
                f"Commands: {_commands_from(goal['status'])}",
            ]
        )
    )


class GoalCommand(Service):
    """Registers ``/goal``."""

    provide = "command_goal"
    inject = ["commands", "goals"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        dispose = ctx.commands.register(
            "goal", "Set or view the objective for a long-running task", self._run
        )
        ctx.effect(lambda: dispose)

    def _render(self, title: str, session: Any, goal: dict) -> CommandResult:
        goals = self.ctx.goals
        return render_goal(title, goal, goals.rounds(session), goals.max_rounds)

    def _run(self, invocation: Any) -> CommandResult:
        agent = invocation.agent
        session = getattr(agent, "session", None)
        if session is None:
            return CommandResult.error(f"/goal needs a session to act on. {USAGE}")

        goals = self.ctx.goals
        command = parse_goal_command(invocation.raw_input)
        kind = command["kind"]

        try:
            current = goals.current(session)

            if kind == "show":
                if current is None:
                    return CommandResult.success(f"No goal is set.\n{USAGE}")
                return self._render("Goal", session, current)

            if kind == "invalid-edit":
                return CommandResult.error(
                    f"Editing a goal needs a replacement objective.\n{USAGE}"
                )

            if kind == "create":
                if current is not None and current["status"] == "active":
                    return CommandResult.error(
                        "A goal is already active. Use /goal edit <objective> to "
                        "change it, or /goal clear before setting another."
                    )
                return self._render(
                    "Goal set", session, goals.set(session, command["objective"])
                )

            if kind == "edit":
                if current is None:
                    return self._missing("edit")
                return self._render(
                    "Goal updated", session, goals.set(session, command["objective"])
                )

            if kind in ("pause", "resume"):
                if current is None:
                    return self._missing(kind)
                return self._render(
                    f"Goal {kind}d", session, goals.transition(session, kind)
                )

            if kind == "clear":
                if current is None:
                    return CommandResult.success("There is no goal to clear.")
                goals.clear(session)
                return CommandResult.success("Goal cleared.")

        except GoalError as error:
            # Two things reach here: a change the fold refuses, and a *read*
            # that fails because the log holds a change this version cannot
            # decode. Both are the person's problem to know about and neither
            # is theirs to fix, so the sentence covers both without pretending
            # to diagnose. The code is on the exception, for a client that
            # wants to branch.
            return CommandResult.error(
                f"The goal could not be read or changed: {error}. "
                "Run /goal to see what is available."
            )

        return CommandResult.error(f"Unrecognised goal command.\n{USAGE}")

    @staticmethod
    def _missing(action: str) -> CommandResult:
        return CommandResult.error(
            f"No goal is set, and /goal {action} needs one. {USAGE}"
        )


__all__ = ["GoalCommand", "parse_goal_command", "render_goal", "USAGE", "CONTROLS"]
