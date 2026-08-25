"""Guards — plugins that watch the tool pipeline without owning it.

Two of them, and both are shaped by the same judgement: **advise, do not
veto.**

A guard that silently blocks a call leaves the model repeating something that
now mysteriously does nothing, with no way to tell why. Telling it what is
happening is more useful and more honest — and plugkit's pipeline already has a
place (guards returning a reason, approvers) for a consumer that really does
want to refuse.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from typing import Any, Optional

from plugkit import Service

from ..bounded import TextRetainer, format_retention_notice
from ..message import MessageSource, TextBlock, create_user_message

logger = logging.getLogger("pydsh.tools.guards")

#: Consecutive identical calls at which the model is reminded. Escalating
#: rather than a single point: the first nudge is gentle, later ones are not.
DEFAULT_THRESHOLDS = (3, 5, 8)

#: Marks a plugin-authored message so a renderer does not show it as the user
#: speaking. Load-bearing: an untagged notice reads as a user instruction.
NOTICE_FORM = "notice"

GENTLE_REMINDER = (
    "You are repeating the same tool call with identical arguments. Read the "
    "previous result carefully before calling again: if the task is not done, "
    "try a different approach or different arguments rather than repeating."
)


def detailed_reminder(tool: str, count: int, arguments: str) -> str:
    """A firmer reminder, naming what is being repeated."""
    return (
        "Repeated tool call detected:\n"
        f"- tool: {tool}\n"
        f"- consecutive calls: {count}\n"
        f"- arguments: {arguments}\n"
        "These calls are not making progress. Do not call this tool with these "
        "arguments again — inspect the last result and choose a different "
        "action, different arguments, or finish."
    )


def canonical_arguments(arguments: Any) -> str:
    """Arguments as a string where only *meaning* differs, not key order.

    Models vary key order between otherwise identical calls. Comparing raw
    strings under-counts exactly the loop this guard exists to catch, and it
    fails *open* — producing no signal at all rather than a wrong one.
    """
    try:
        return json.dumps(arguments, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(arguments)


def _matches(name: str, patterns: Optional[list[str]]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns or [])


def resolve_thresholds(raw: Any) -> tuple[int, ...]:
    """Validate the escalation points at load time, not at the first repeat."""
    values = tuple(raw) if raw is not None else DEFAULT_THRESHOLDS
    if not values:
        raise ValueError("repeat-tool guard: thresholds must not be empty")
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"repeat-tool guard: threshold {value!r} is not an integer")
        if value < 2:
            raise ValueError(
                f"repeat-tool guard: threshold {value} is below 2 — a single call "
                "cannot be a repetition"
            )
    if len(set(values)) != len(values):
        raise ValueError(f"repeat-tool guard: thresholds {values} contain a duplicate")
    return tuple(sorted(values))


class RepeatToolGuard(Service):
    """Notices a model calling one tool with the same arguments, over and over."""

    provide = "repeat_tool_guard"
    inject = ["tools"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        self.thresholds = resolve_thresholds(config.get("thresholds"))
        self._include = config.get("include")
        self._exclude = config.get("exclude")
        #: Per agent: the last call seen and how many times in a row.
        self._streaks: dict[int, dict] = {}

        ctx.on("tools/result", self._observe)
        # A user's message changes the situation, so repetition across it is
        # not a loop.
        ctx.on("agent/inbox/claimed", self._reset)

    def _watched(self, name: str) -> bool:
        if self._include is not None and not _matches(name, self._include):
            return False
        return not _matches(name, self._exclude)

    def _reset(self, agent: Any, message: Any = None, turn: Any = None) -> None:
        self._streaks.pop(id(agent), None)

    def _observe(self, execution: Any, result: Any = None) -> None:
        """Count a completed call — whatever its outcome.

        Counted after execution, and regardless of whether it succeeded: a
        model hammering a wall is looping just as much as one repeating a
        success, and a rejected call is exactly the case worth interrupting.
        """
        try:
            name = execution.name
            if not self._watched(name):
                return
            agent = getattr(execution, "caller", None)
            if agent is None:
                return

            key = (name, canonical_arguments(execution.arguments))
            streak = self._streaks.get(id(agent))
            if streak is not None and streak["key"] == key:
                streak["count"] += 1
            else:
                streak = {"key": key, "count": 1}
                self._streaks[id(agent)] = streak

            if streak["count"] in self.thresholds:
                self._remind(agent, name, streak["count"], key[1])
        except Exception as exc:  # noqa: BLE001 - a guard is not the call
            logger.warning("repeat-tool guard failed: %s", exc, exc_info=exc)

    def _remind(self, agent: Any, tool: str, count: int, arguments: str) -> None:
        text = (
            GENTLE_REMINDER
            if count == self.thresholds[0]
            else detailed_reminder(tool, count, arguments)
        )
        message = create_user_message(
            [TextBlock(text)],
            source=MessageSource("plugin", plugin="repeat-tool-guard", form=NOTICE_FORM),
        )
        # Delivered as a follow-up, not inserted as a user turn: the source
        # marks it as plugin-authored, which is what stops a renderer showing
        # it as something the user said.
        agent.followup(message)


class SpillPolicy(Service):
    """Sends an oversized tool result to the spill store and returns a locator."""

    provide = "spill_policy"
    inject = ["tools"]

    #: Characters a result may reach before the whole thing is spilled.
    DEFAULT_THRESHOLD_CHARS = 16_384

    #: Bytes of excerpt returned alongside the locator.
    DEFAULT_HEAD_BYTES = 4_096
    DEFAULT_TAIL_BYTES = 1_024

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        self._threshold = int(config.get("threshold_chars", self.DEFAULT_THRESHOLD_CHARS))
        self._head = int(config.get("head_bytes", self.DEFAULT_HEAD_BYTES))
        self._tail = int(config.get("tail_bytes", self.DEFAULT_TAIL_BYTES))
        self._root = getattr(ctx, "root", ctx)
        ctx.on("tools/post-execute", self._maybe_spill)

    async def _maybe_spill(self, execution: Any, result: Any, next_: Any = None) -> Any:
        """Stage 4: decide what to do with a finished result.

        The value chained through this waterfall is a *decision* — the default
        inner returns ``Accept()`` — while the result itself is the second
        argument. Reading ``.value`` off the decision finds ``None`` on every
        call, so the policy silently never fires: the contract compiles and the
        feature does nothing.
        """
        decision = await next_() if next_ is not None else None
        try:
            return await self._spill(execution, result, decision)
        except Exception as exc:  # noqa: BLE001
            # A spill that failed must not lose the result it was preserving.
            logger.warning("spill policy failed: %s", exc, exc_info=exc)
            return decision

    async def _spill(self, execution: Any, result: Any, decision: Any) -> Any:
        value = getattr(result, "value", None)
        if not isinstance(value, str) or len(value) <= self._threshold:
            return decision

        store = getattr(self._root, "spill", None)
        if store is None:
            return decision  # a composition without spilling is a choice

        agent = getattr(execution, "caller", None)
        session_id = getattr(agent, "id", "no-session")
        saved = await store.save_text(
            session_id, f"{execution.name}-{execution.id}.txt", value
        )

        retainer = TextRetainer.head_tail(self._head, self._tail)
        retainer.push(value)
        kept = retainer.finish()
        notice = format_retention_notice(
            {"omitted": kept["omitted_bytes"], "unit": "bytes"},
            lambda n: f"The whole output is at {saved['locator']}. {saved['retrieval_hint']}",
        )

        from plugkit import Accept

        return Accept.replacing(f"{kept['text']}\n\n{notice}")


__all__ = [
    "RepeatToolGuard",
    "SpillPolicy",
    "canonical_arguments",
    "resolve_thresholds",
    "detailed_reminder",
    "GENTLE_REMINDER",
    "DEFAULT_THRESHOLDS",
    "NOTICE_FORM",
]
