"""``ctx.commands`` — what a user can invoke without spending a model turn.

``/plan off``, ``/compact``, ``/feedback``. A command runs its handler directly
against the agent and comes back with text; no model call, no tokens.

The rule that shapes the whole service: **a command never raises at its
caller.** The caller is a person who typed a slash-command, so a handler that
fails, returns the wrong thing, or does not exist all come back as an error
*result*. An exception there would surface as a crash where the user expects a
message.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, Optional

from plugkit import Service


@dataclass(frozen=True)
class CommandInvocation:
    """One invocation's context."""

    agent: Any = None
    signal: Any = None
    command_id: Optional[str] = None
    #: The argument text after the command name. A command that takes no
    #: arguments should check this is empty rather than ignore it.
    raw_input: str = ""


@dataclass(frozen=True)
class CommandResult:
    """What a command comes back with."""

    kind: str  # "success" | "error"
    text: str
    #: The seq of the event the command landed, when it wrote one — a caller
    #: can point at what changed.
    source_event_seq: Optional[int] = None

    @staticmethod
    def success(text: str, source_event_seq: Optional[int] = None) -> "CommandResult":
        return CommandResult("success", text, source_event_seq)

    @staticmethod
    def error(text: str) -> "CommandResult":
        return CommandResult("error", text)


#: A handler, sync or async.
CommandHandler = Callable[[CommandInvocation], Any]


class Commands(Service):
    """Provides ``ctx.commands``."""

    provide = "commands"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._commands: dict[str, dict] = {}

    def register(
        self, name: str, description: str, handler: CommandHandler
    ) -> Callable[[], bool]:
        """Register a command; returns a disposer.

        A repeat registration replaces: a plugin reloading should take its
        command back over rather than collide with its own previous self.
        """
        if not name:
            raise ValueError("a command needs a name")
        entry = {"name": name, "description": description, "handler": handler}
        self._commands[name] = entry

        def dispose() -> bool:
            if self._commands.get(name) is entry:
                del self._commands[name]
                return True
            return False

        return dispose

    def has(self, name: str) -> bool:
        return name in self._commands

    def list(self) -> list[dict]:
        """Every command's name and description, sorted."""
        return [
            {"name": entry["name"], "description": entry["description"]}
            for entry in sorted(self._commands.values(), key=lambda e: e["name"])
        ]

    async def invoke(
        self,
        name: str,
        agent: Any = None,
        signal: Any = None,
        command_id: Optional[str] = None,
        raw_input: str = "",
    ) -> CommandResult:
        """Run a command. Never raises — a failure comes back as an error result."""
        entry = self._commands.get(name)
        if entry is None:
            known = ", ".join(f"/{n}" for n in sorted(self._commands)) or "none"
            return CommandResult.error(f"unknown command /{name} (available: {known})")

        invocation = CommandInvocation(
            agent=agent, signal=signal, command_id=command_id, raw_input=raw_input
        )
        try:
            result = entry["handler"](invocation)
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, CommandResult):
                return CommandResult.error(
                    f"/{name} returned {type(result).__name__}, not a CommandResult"
                )
            return result
        except Exception as exc:  # noqa: BLE001 - a person typed this
            return CommandResult.error(f"/{name} failed: {exc}")


__all__ = ["Commands", "CommandInvocation", "CommandResult", "CommandHandler"]
