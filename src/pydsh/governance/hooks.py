"""``ctx.hooks`` — letting a deployment run its own commands inside the loop.

This is the seam an operator uses to enforce policy the harness knows nothing
about: check something before a tool runs, react after a turn ends. The
commands are the deployment's, not ours.

The judgement in this module is **merging**. Several hooks answer one question,
and the rule is restraint: any block wins, an ask survives unless something
blocked, and nothing is ever moved toward allow. A merge that let two
permissive hooks outvote one restrictive one would make whether an operator's
"no" takes effect depend on how many *other* hooks happen to be installed —
not a policy anyone can reason about, and it fails silently.

Hooks run through :mod:`pydsh.capability.shell`, inheriting its timeout and
process-group termination, so a hook that hangs is stopped along with anything
it started.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from plugkit import Service

logger = logging.getLogger("pydsh.hooks")

#: How long a hook may run unless it says otherwise. Ten minutes matches the
#: reference protocol, which the external tools were written against.
DEFAULT_HOOK_TIMEOUT_MS = 600_000

#: Characters of a hook's stderr kept in the durable record. Enough to diagnose
#: it; not enough for a chatty script to fill the log.
DEFAULT_STDERR_SUMMARY_MAX_CHARS = 2_000

#: The exit code the protocol reads as "block", for hooks that do not emit JSON.
BLOCKING_EXIT_CODE = 2

DECISIONS = ("allow", "ask", "block", "deny")


@dataclass
class HookOutput:
    """One hook's answer, however it chose to express it."""

    exit_code: int = 0
    decision: Optional[str] = None
    reason: Optional[str] = None
    stop_reason: Optional[str] = None
    continue_flag: Optional[bool] = None
    additional_context: Optional[str] = None
    system_message: Optional[str] = None
    updated_input: Optional[Any] = None
    stderr: str = ""


@dataclass
class MergedOutcome:
    """What several hooks together decided."""

    decision: str = "allow"
    block_reason: Optional[str] = None
    additional_contexts: list = field(default_factory=list)
    system_messages: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def matches(matcher: Optional[str], value: str, regex: bool = False) -> bool:
    """Whether a hook applies to this call.

    A literal matcher is a ``|``-separated alternation, which is what the
    external tools' configuration files use. A regex matcher is opt-in, so a
    tool name containing a metacharacter is not accidentally a pattern.
    """
    if not matcher:
        return True  # no matcher: every call
    if regex:
        try:
            return re.search(matcher, value) is not None
        except re.error:
            logger.warning("hooks: matcher %r is not a valid pattern", matcher)
            return False
    return value in {part.strip() for part in matcher.split("|") if part.strip()}


def summarize_stderr(stderr: str, max_chars: int = DEFAULT_STDERR_SUMMARY_MAX_CHARS) -> str:
    """A hook's stderr, bounded for the durable record (I4)."""
    stderr = stderr or ""
    if len(stderr) <= max_chars:
        return stderr
    return f"{stderr[:max_chars]}… (+{len(stderr) - max_chars} more characters)"


def parse_hook_output(exit_code: int, stdout: str, stderr: str) -> HookOutput:
    """Decode a hook's answer from what it printed and how it exited.

    JSON on stdout is the rich form. A non-zero exit with no JSON is read as a
    **block** carrying the stderr — the conservative reading, and the one that
    lets a plain shell script participate without knowing the protocol.
    """
    output = HookOutput(exit_code=exit_code, stderr=stderr or "")

    body: Any = None
    text = (stdout or "").strip()
    if text:
        try:
            body = json.loads(text)
        except ValueError:
            body = None  # not protocol-speaking; the exit code decides

    if isinstance(body, dict):
        decision = body.get("decision")
        output.decision = decision if decision in DECISIONS else None
        output.reason = body.get("reason")
        output.stop_reason = body.get("stopReason") or body.get("stop_reason")
        if "continue" in body:
            output.continue_flag = bool(body["continue"])
        output.additional_context = body.get("additionalContext") or body.get("additional_context")
        output.system_message = body.get("systemMessage") or body.get("system_message")
        output.updated_input = body.get("updatedInput", body.get("updated_input"))
        return output

    if exit_code == BLOCKING_EXIT_CODE or exit_code != 0:
        output.decision = "block"
        output.reason = summarize_stderr(stderr) or f"the hook exited {exit_code}"
    return output


def merge_hook_outputs(outputs: list) -> MergedOutcome:
    """Combine several answers, conservatively (I3).

    Never softened. The first block's reason is the one reported, so the
    outcome does not depend on the order hooks happen to finish in.
    """
    outcome = MergedOutcome()
    saw_ask = False

    for output in outputs:
        blocking = output.continue_flag is False or output.decision in ("block", "deny")
        if blocking and outcome.decision != "block":
            outcome.decision = "block"
            outcome.block_reason = (
                output.stop_reason or output.reason or "a hook blocked the operation"
            )
        if output.decision == "ask":
            saw_ask = True
        if output.additional_context:
            outcome.additional_contexts.append(output.additional_context)
        if output.system_message:
            outcome.system_messages.append(output.system_message)
        if output.updated_input is not None:
            # Recorded and refused. Honouring it would let a hook rewrite the
            # call the harness is about to make, which is a much larger power
            # than "approve or refuse" and needs its own design.
            outcome.warnings.append(
                "a hook asked to rewrite the call's input; recorded and not honoured"
            )

    if outcome.decision != "block" and saw_ask:
        outcome.decision = "ask"
    return outcome


class HooksProtocol(Service):
    """Provides ``ctx.hooks`` — the neutral protocol, no dialect."""

    provide = "hooks"
    inject = ["shell"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        self._timeout_ms = int(config.get("timeout_ms", DEFAULT_HOOK_TIMEOUT_MS))
        #: point -> list of hook definitions
        self._hooks: dict[str, list[dict]] = {}
        self._detached: set = set()

    def register(self, point: str, hook: dict) -> Any:
        """Register a hook at a point; returns a disposer."""
        if not hook.get("command"):
            raise ValueError("a hook needs a command")
        entry = dict(hook)
        self._hooks.setdefault(point, []).append(entry)

        def dispose() -> bool:
            hooks = self._hooks.get(point, [])
            for index, candidate in enumerate(hooks):
                if candidate is entry:
                    hooks.pop(index)
                    return True
            return False

        return dispose

    def hooks_for(self, point: str, value: str = "") -> list[dict]:
        return [
            hook
            for hook in self._hooks.get(point, [])
            if matches(hook.get("matcher"), value, bool(hook.get("regex")))
        ]

    async def run_hook(self, hook: dict, payload: Any, signal: Any = None) -> HookOutput:
        """Run one hook and decode its answer. Never raises."""
        try:
            result = await self.ctx.shell.execute(
                hook["command"],
                cwd=hook.get("cwd"),
                timeout_ms=int(hook.get("timeout_ms", self._timeout_ms)),
                env={"PYDSH_HOOK_PAYLOAD": json.dumps(payload, ensure_ascii=False)},
                signal=signal,
            )
        except Exception as error:  # noqa: BLE001 - a broken hook is a decision
            return HookOutput(
                exit_code=-1, decision="block",
                reason=f"the hook could not be run: {error}",
            )
        return parse_hook_output(
            result["exit_code"], result["stdout"], result["stderr"]
        )

    async def run_point(self, point: str, value: str, payload: Any, signal: Any = None) -> MergedOutcome:
        """Run every hook matching this point, and merge their answers."""
        hooks = self.hooks_for(point, value)
        if not hooks:
            return MergedOutcome()
        outputs = [await self.run_hook(hook, payload, signal) for hook in hooks]
        return merge_hook_outputs(outputs)

    def run_detached(self, hook: dict, payload: Any) -> None:
        """Start a hook whose answer nobody waits for.

        Tracked so it is not garbage-collected mid-run and so a failure is
        logged rather than vanishing into an unretrieved-task warning.
        """
        try:
            task = asyncio.ensure_future(self.run_hook(hook, payload))
        except RuntimeError:
            return
        self._detached.add(task)
        task.add_done_callback(self._detached.discard)

    async def drain(self) -> None:
        """Wait for detached runs — for tests and shutdown."""
        while self._detached:
            await asyncio.gather(*list(self._detached), return_exceptions=True)


__all__ = [
    "HooksProtocol",
    "HookOutput",
    "MergedOutcome",
    "matches",
    "parse_hook_output",
    "merge_hook_outputs",
    "summarize_stderr",
    "DEFAULT_HOOK_TIMEOUT_MS",
    "DEFAULT_STDERR_SUMMARY_MAX_CHARS",
    "BLOCKING_EXIT_CODE",
    "DECISIONS",
]
