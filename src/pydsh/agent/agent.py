"""The turn/step loop — the thing that actually drives a conversation.

A conversation is a sequence of **turns**; a turn is a sequence of **steps**;
a step is one model call plus the tool calls it asked for. That is the whole
machine. Everything else here is bookkeeping to make each of those a fact in
the session log rather than state in memory.

The shape of one step:

1. ``agent/pre-step`` waterfall — plugins decide whether the pending input
   enters, and may add to it. The default decision is to let it in.
2. Build the request from the log (``derive_messages``), the mounted tools, and
   the agent's options; record the route on the session header; stream it.
3. Fold the frames into an assistant message, write it, and run whatever tools
   the model asked for. Their results become the next step's pending input.

Two cancellation scopes, and the difference matters: the **lifetime** signal
ends the agent (its owner unmounted, the loop plugin was torn down), while the
**activity** signal covers one drain and is what :meth:`Agent.cancel` aborts.
The reference has only one, aborts it in ``cancel()``, and — because a signal
never un-aborts — leaves an agent that can never run again. Stopping the work
in flight is not the same request as ending the agent.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Optional

from ..cancel import CancelledError, CancelSignal
from ..dispatch import emit_contained
from ..llm.call_config import call_config_from_options
from ..llm.chunks import GenerateOptions
from ..llm.errors import LlmError
from ..message import (
    Message,
    MessageSource,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    create_assistant_message,
    create_user_message,
    decode_payload,
    encode_payload,
)
from .assembler import BlockAssembler
from .inbox import NEXT_TURN, Inbox

#: How many steps one turn may take before the loop gives up. A step is a model
#: call, so this is the ceiling on runaway tool cycles.
DEFAULT_MAX_STEPS = 32

#: How many of a step's tool calls may run at once. One is serial, which is the
#: safe default: tools that touch a shared working directory are common.
DEFAULT_MAX_PARALLEL_TOOL_CALLS = 1

#: Dispatch names. Named here so a plugin author can import them instead of
#: retyping a string that will not fail loudly when it is wrong.
PRE_STEP = "agent/pre-step"
REQUEST = "agent/request"
REQUEST_ERROR = "agent/request-error"
STATUS = "agent/status"
SESSION_START = "agent/session-start"
INBOX_INSERTED = "agent/inbox/inserted"
INBOX_DISCARDED = "agent/inbox/discarded"
INBOX_CLAIMED = "agent/inbox/claimed"


@dataclass
class AgentOptions:
    """How one agent routes and bounds its work."""

    provider: str = ""
    model: str = ""
    system: str = ""
    max_tokens: Optional[int] = None
    max_steps: int = DEFAULT_MAX_STEPS
    max_parallel_tool_calls: int = DEFAULT_MAX_PARALLEL_TOOL_CALLS


@dataclass(frozen=True)
class _ToolOutcome:
    """One tool call's result, flattened to what the loop writes to the log."""

    text: str
    is_error: bool


class Agent:
    """Drives one session's conversation."""

    def __init__(
        self,
        ctx: Any,
        session: Any,
        options: Optional[AgentOptions] = None,
        source: str = "startup",
        signal: Optional[CancelSignal] = None,
    ) -> None:
        self.ctx = ctx
        # Tools are optional, and plugkit's `inject` cannot express that: it is
        # the requirement list *and* the permission list, so a plugin context
        # that did not inject `tools` cannot read it even when it is mounted —
        # the loop would silently behave as if no tool existed. Declaring it
        # instead would leave the whole loop PENDING (silently absent) for a
        # consumer who mounts no tools. So the optional capability is resolved
        # from the root context, where no permission list applies.
        # ponytail: collapses to plain `ctx.tools` if plugkit grows an optional
        # inject; this is the only place that reaches for the root.
        self._root = getattr(ctx, "root", ctx)
        self.session = session
        # An agent is bound 1:1 to a session, so it takes the session's id
        # rather than minting a second identity that could disagree.
        self.id = session.header.id or session.id
        self.options = options or AgentOptions()
        self.source = source
        # Post-commit notifications, so a throwing observer cannot turn a
        # recorded inbox change back into a failure (the same reason spec 01's
        # session/event broadcast is contained).
        self.inbox = Inbox(
            session,
            {
                "inserted": lambda m: emit_contained(ctx, INBOX_INSERTED, self, m),
                "discarded": lambda m: emit_contained(ctx, INBOX_DISCARDED, self, m),
                "claimed": lambda m, t: emit_contained(ctx, INBOX_CLAIMED, self, m, t),
            },
        )
        # The agent is over when this aborts. Fused so an owner's teardown
        # propagates; disposed with the agent so the owner does not keep a
        # listener per agent it ever created.
        self._lifetime = CancelSignal.any([signal])
        # One drain's scope. Replaced each drain, which is why cancelling does
        # not permanently disable the agent.
        self._activity: Optional[CancelSignal] = None
        self._draining: Optional[asyncio.Task] = None
        # Turn numbers are read from the log once and counted in memory after,
        # rather than rescanning every event on each turn.
        self._last_turn = _last_turn_number(session)
        emit_contained(ctx, SESSION_START, {"agent": self, "source": source})

    # -- delivering input --------------------------------------------------- #
    def insert(self, message: Message, target: str = NEXT_TURN) -> None:
        """Deliver a message and start processing if the agent is idle.

        Fire-and-forget: it returns as soon as the message is in the inbox.
        Use :meth:`run` when you need to wait for the answer.
        """
        self.inbox.append(target, message)
        self._ensure_draining()

    def followup(self, message: Message) -> None:
        """Deliver a plugin-sourced message as a follow-up.

        Same delivery as :meth:`insert`. It is a separate name because the
        message's *source* is what plugins key on — a scheduled reminder is not
        the user interrupting, and a guard counting repeats must not treat it
        as one.
        """
        self.insert(message)

    async def run(self, user_text: str) -> None:
        """Deliver one user message and wait until processing finishes.

        Waits for the drain in flight when there already is one — the message
        went into the same inbox that drain is emptying, so returning early
        would be returning before the work was done.
        """
        message = create_user_message([TextBlock(user_text)], MessageSource("user"))
        self.inbox.append(NEXT_TURN, message)
        await asyncio.shield(self._ensure_draining())

    # -- cancelling and waiting --------------------------------------------- #
    def cancel(self, cause: Any = None) -> None:
        """Stop the work in flight. The agent stays usable afterwards."""
        if self._activity is not None:
            self._activity.abort(cause)

    async def when_idle(self) -> None:
        """Wait for any background drain to finish."""
        task = self._draining
        if task is not None and not task.done():
            await asyncio.shield(task)

    def dispose(self) -> None:
        """End this agent for good and detach it from its owner's signal."""
        self._lifetime.abort("agent disposed")
        self._lifetime.dispose()

    # -- the drain ---------------------------------------------------------- #
    def _ensure_draining(self) -> "asyncio.Future":
        """The drain in flight, starting one if there is none.

        The single entry point, so two deliveries can never race into two
        concurrent drains over one inbox. The activity signal is created *here*
        rather than inside the task, because a caller may deliver and cancel
        before the event loop ever gets round to running the drain — with the
        signal created late, that cancellation would land on nothing.
        """
        if self._draining is None or self._draining.done():
            activity = CancelSignal.any([self._lifetime])
            self._activity = activity
            self._draining = asyncio.ensure_future(self._drain(activity))
        return self._draining

    async def _drain(self, activity: CancelSignal) -> None:
        """Process the inbox until it is empty or the activity is cancelled."""
        emit_contained(self.ctx, STATUS, {"agent": self, "status": "running"})
        try:
            while self.inbox.has_pending:
                activity.throw_if_aborted()
                turn = self._last_turn + 1
                claimed = self.inbox.claim(NEXT_TURN, turn)
                if not claimed:
                    # has_pending was true but nothing could be claimed. Not
                    # expected; breaking beats spinning on it forever.
                    break
                self._last_turn = turn
                await self._run_turn(turn, claimed, activity)
        except CancelledError:
            pass  # the turn already recorded why it stopped
        finally:
            if self._activity is activity:
                self._activity = None
            activity.dispose()
            emit_contained(self.ctx, STATUS, {"agent": self, "status": "idle"})

    # -- one turn ----------------------------------------------------------- #
    async def _run_turn(
        self, turn: int, claimed: list[Message], signal: CancelSignal
    ) -> None:
        """Run steps until the turn ends, and always close it (I1)."""
        self.session.append("turn/start", {"turn": turn})
        step = 0
        pending = list(claimed)
        reason: Optional[dict] = None
        try:
            while True:
                signal.throw_if_aborted()
                if step >= self.options.max_steps:
                    # Distinct from the model's own token ceiling: the loop ran
                    # out of steps, which is a different thing to explain.
                    reason = {"kind": "max-steps"}
                    break
                step += 1

                decision = await self._pre_step(pending, turn, step, signal)
                if decision.get("kind") == "reject":
                    reason = {"kind": "blocked"}
                    break

                for message in decision.get("messages") or []:
                    self.session.append("user/message", encode_payload(message))

                self.session.append("step/start", {"turn": turn, "step": step})
                try:
                    reason, results = await self._run_step(turn, step, signal)
                finally:
                    self.session.append("step/end", {"turn": turn, "step": step})
                # Check again now the step is over. An adapter is expected to
                # honour the signal mid-stream, but one that does not must not
                # get its turn recorded as a clean completion — the user asked
                # to stop, and the log has to say that is what happened.
                signal.throw_if_aborted()
                if reason is not None:
                    break
                # A step that did not end the turn ran tools; their results are
                # what the next step carries in.
                pending = results
        except CancelledError as exc:
            reason = {"kind": "cancelled", "reason": exc.reason}
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            # Not swallowed: the caller still gets the exception. But the log
            # must not say "completed" about a turn that blew up — a reader
            # cannot tell the difference afterwards, and that is the whole
            # value of the log.
            reason = {"kind": "failed", "error": f"{type(exc).__name__}: {exc}"}
            raise
        finally:
            self.session.append(
                "turn/end", {"turn": turn, "reason": reason or {"kind": "completed"}}
            )

    async def _pre_step(
        self, pending: list[Message], turn: int, step: int, signal: CancelSignal
    ) -> dict:
        """Ask the plugins whether this input enters, and what else comes with it."""

        async def default_decision() -> dict:
            return {"kind": "enter", "messages": list(pending)}

        decision = await self.ctx.waterfall(
            PRE_STEP,
            {
                "agent": self,
                "messages": list(pending),
                "turn": turn,
                "step": step,
                "signal": signal,
            },
            default_decision,
        )
        # A listener that returns nothing has not decided anything; treat that
        # as "no opinion" rather than silently dropping the user's input.
        return decision if isinstance(decision, dict) else {"kind": "enter", "messages": list(pending)}

    # -- one step ----------------------------------------------------------- #
    async def _run_step(
        self, turn: int, step: int, signal: CancelSignal
    ) -> tuple[Optional[dict], list[Message]]:
        """Stream one model call and run what it asked for.

        Returns the turn-end reason (or ``None`` to keep stepping) and, in that
        second case, the tool results the next step carries in.
        """
        await self.ctx.parallel(
            REQUEST, {"agent": self, "turn": turn, "step": step, "signal": signal}
        )
        options = self._build_request(signal)
        # The route this epoch is running under, kept on the header so a
        # resumed session continues on the same provider and model.
        self.session.header.request = call_config_from_options(options)

        assembler = await self._stream(options, turn, step, signal)
        assembler.finalize()

        finish = assembler.finish
        if finish.get("kind") == "error":
            # No assistant message: there is no reply, and writing an empty one
            # would put a turn in the model's history that never happened.
            return finish, []

        message = create_assistant_message(
            assembler.blocks, provider=options.provider, model=options.model
        )
        self.session.append(
            "assistant/message",
            {
                "turn": turn,
                "step": step,
                "message": encode_payload(message),
                "usage": assembler.usage,
            },
        )

        if finish.get("kind") == "max-tokens":
            return {"kind": "max-tokens"}, []

        calls = assembler.tool_calls()
        if not calls:
            return {"kind": "completed"}, []

        results = await self._execute_tool_calls(calls, turn, step, signal)
        return None, results

    def _build_request(self, signal: CancelSignal) -> GenerateOptions:
        """Assemble the model request from the log and the mounted services."""
        # The log holds encoded payloads; the adapter speaks the vocabulary.
        messages = [decode_payload(m) for m in self.session.derive_messages()]
        return GenerateOptions(
            provider=self.options.provider,
            model=self.options.model,
            messages=messages,
            system=self.options.system or None,
            tools=self._tool_schemas(),
            max_tokens=self.options.max_tokens,
            signal=signal,
            session_id=self.id,
        )

    def _tools(self) -> Any:
        """The tools service, or ``None`` when none is mounted."""
        return getattr(self._root, "tools", None)

    def _tool_schemas(self) -> Optional[list[dict]]:
        """The registered tools as the model sees them, or None if none are."""
        tools = self._tools()
        if tools is None:
            return None
        schemas = [
            {
                "name": tool.name,
                "description": getattr(tool, "description", "") or "",
                "parameters": getattr(tool, "parameters", None) or {},
            }
            for tool in tools.list()
        ]
        # An empty list and "no tools service" are different states, but both
        # mean the same thing to a provider — do not send an empty tool list.
        return schemas or None

    async def _stream(
        self, options: GenerateOptions, turn: int, step: int, signal: CancelSignal
    ) -> BlockAssembler:
        """Stream one call, with one recovery attempt per failure.

        ``agent/request-error`` lets a plugin fix the condition — compaction
        shrinking an overflowing context is the motivating case — and answer
        ``retry``. The assembler is rebuilt for the retry, because the frames
        from the failed attempt are not part of the new reply.
        """
        while True:
            assembler = BlockAssembler()
            try:
                async for chunk in self.ctx.llm.stream(options):
                    self.session.append(
                        "assistant/chunk",
                        {"turn": turn, "step": step, "chunk": encode_payload(chunk)},
                    )
                    assembler.push(chunk)
                return assembler
            except LlmError as exc:
                async def no_recovery() -> Any:
                    return None

                decision = await self.ctx.waterfall(
                    REQUEST_ERROR,
                    {"agent": self, "failure": exc, "signal": signal},
                    no_recovery,
                )
                if isinstance(decision, dict) and decision.get("kind") == "retry":
                    continue
                raise

    # -- tools --------------------------------------------------------------- #
    async def _execute_tool_calls(
        self, calls: list[ToolCallBlock], turn: int, step: int, signal: CancelSignal
    ) -> list[Message]:
        """Run a step's tool calls; write them all in call order (I4).

        Concurrency is bounded, but the log is not: whichever call finishes
        first, the events land in the order the model asked, so a replay of the
        same conversation reads the same way every time. Returns the result
        messages, which are the next step's input.
        """
        gate = asyncio.Semaphore(max(1, self.options.max_parallel_tool_calls))

        async def run_one(call: ToolCallBlock) -> _ToolOutcome:
            async with gate:
                return await self._execute_one(call, signal)

        outcomes = await asyncio.gather(*(run_one(call) for call in calls))
        results: list[Message] = []
        for call, outcome in zip(calls, outcomes):
            self.session.append(
                "tool/call",
                {
                    "turn": turn,
                    "step": step,
                    "callId": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                },
            )
            result_message = create_user_message(
                [
                    ToolResultBlock(
                        tool_call_id=call.id,
                        content=(TextBlock(outcome.text),),
                        is_error=outcome.is_error,
                    )
                ],
                source=MessageSource("tool"),
            )
            self.session.append(
                "tool/result",
                {
                    "turn": turn,
                    "step": step,
                    "message": encode_payload(result_message),
                    "error": outcome.is_error,
                    "meta": None,
                },
            )
            results.append(result_message)
        return results

    async def _execute_one(self, call: ToolCallBlock, signal: CancelSignal) -> _ToolOutcome:
        """Run one tool call. Never raises — a failure is a result (I3)."""
        arguments, parse_error = _parse_arguments(call.arguments)
        if parse_error is not None:
            return _ToolOutcome(parse_error, True)

        tools = self._tools()
        if tools is None:
            return _ToolOutcome(
                f"no tools are available, so {call.name!r} cannot be called", True
            )

        result = await tools.execute(call.name, arguments, caller=self, id=call.id)
        if getattr(result, "ok", False):
            return _ToolOutcome(_as_text(result.value), False)
        error = getattr(result, "error", None) or {}
        message = error.get("message") or "the tool failed without saying why"
        code = error.get("code")
        return _ToolOutcome(f"{code}: {message}" if code else message, True)

def _parse_arguments(text: str) -> tuple[dict, Optional[str]]:
    """Parse a model's argument text into the dict the tool pipeline wants.

    Models emit malformed or non-object argument text often enough that this is
    a normal path, not an exceptional one. The failure is phrased for the model
    to read, because that is who gets it back.
    """
    if not text or not text.strip():
        return {}, None
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        return {}, f"the arguments were not valid JSON: {exc}"
    if not isinstance(parsed, dict):
        return {}, (
            f"the arguments must be a JSON object, got {type(parsed).__name__}"
        )
    return parsed, None


def _as_text(value: Any) -> str:
    """A tool's return value as the text the model will read."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _last_turn_number(session: Any) -> int:
    """The highest turn number already in the log, or zero."""
    last = 0
    for event in session.events:
        if event.type == "turn/start":
            last = max(last, event.data.get("turn", 0))
    return last


__all__ = [
    "Agent",
    "AgentOptions",
    "DEFAULT_MAX_STEPS",
    "DEFAULT_MAX_PARALLEL_TOOL_CALLS",
    "PRE_STEP",
    "REQUEST",
    "REQUEST_ERROR",
    "STATUS",
    "SESSION_START",
]
