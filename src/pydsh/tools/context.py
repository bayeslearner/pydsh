"""Context injectors — small plugins carrying the sprint's main idea.

Both add a fact the model needs at the start of a turn: what time it is, what
the working instructions are. Both do it by injecting a **message**, not by
touching the system prompt, and that choice is the whole point.

A system prompt is stable across a conversation. The time is not. Rewriting the
prompt every turn invalidates every prompt cache, makes the prompt un-diffable,
and means "what was the model told" has a different answer at every step. As
*history*, each new snapshot simply supersedes the last — the same mechanism
compaction uses, and the reason the surface exists at all.

The reference calls these the demonstration that the injection seam works. A
port that kept `agent/pre-step` and skipped these would keep the seam and lose
the only proof of it.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from plugkit import Service

from ..message import MessageSource, TextBlock, create_user_message

#: Marks a plugin-authored snapshot, so a renderer does not show it as the user
#: speaking and a guard does not count it as a user interruption.
SNAPSHOT_FORM = "snapshot"

#: Prefixed so the model reads a snapshot as context rather than instruction.
TIME_PREFIX = "Current time: "


class _FirstStepInjector(Service):
    """Injects one message on the first step of each turn.

    First step only. A snapshot taken at step one is still true at step three,
    and re-injecting it would fill the history with near-identical messages
    that cost tokens and say nothing new.
    """

    #: Set by a subclass; used for the message's plugin tag.
    plugin_name = "injector"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self.config = config or {}
        ctx.on("agent/pre-step", self._inject)

    def text_for(self, payload: dict) -> Optional[str]:
        """What to inject, or ``None`` for nothing this step."""
        raise NotImplementedError

    async def _inject(self, payload: dict, next_: Any) -> Any:
        decision = await next_()
        if not isinstance(decision, dict) or decision.get("kind") != "enter":
            return decision
        if payload.get("step") != 1:
            return decision

        text = self.text_for(payload)
        if not text:
            return decision

        snapshot = create_user_message(
            [TextBlock(text)],
            source=MessageSource(
                "plugin", plugin=self.plugin_name, form=SNAPSHOT_FORM
            ),
        )
        # Prepended: the context should be in front of what the user said, the
        # way a briefing precedes a question.
        return {
            "kind": "enter",
            "messages": [snapshot, *(decision.get("messages") or [])],
        }


class TimeContext(_FirstStepInjector):
    """Tells the model what time it is, once per turn."""

    provide = "time_context"
    plugin_name = "time-context"

    def text_for(self, payload: dict) -> Optional[str]:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())
        return f"{TIME_PREFIX}{stamp}"


class SystemInstructions(_FirstStepInjector):
    """Injects a deployment's working instructions as history.

    Deliberately *not* a prompt section, even though it reads like one. As
    history it can be superseded, compacted, and seen in the log next to what
    the model did with it — none of which is true of prompt text.
    """

    provide = "system_instructions"
    plugin_name = "system-instructions"

    def text_for(self, payload: dict) -> Optional[str]:
        return self.config.get("instructions") or None


__all__ = ["TimeContext", "SystemInstructions", "SNAPSHOT_FORM", "TIME_PREFIX"]
