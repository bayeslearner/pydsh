"""``ctx.schedules`` — the timer as a projection of the durable log.

The log is the truth; the timer is an approximation of it. Every wake-up
re-folds and re-checks the wall clock against the durable target, and refuses
to deliver early. A timer alone fires at the wrong moment after a clock
adjustment, a long pause, or a suspended laptop — and an early delivery is
indistinguishable, to the model, from a correct one.

**Delivery is session-local, and this says so.** A reminder fires on time while
the session is alive; otherwise it is *overdue*, delivered when the session
comes back and labelled as late. The alternative is a daemon delivering into
sessions nobody is running, which is a far larger promise about process
lifetime — and half-making it is worse than not making it, because a user who
believes a reminder will fire and finds it did not is worse off than one who
was told it is best-effort.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from plugkit import Service

from ..message import MessageSource, TextBlock, create_user_message, encode_payload
from .schedule_domain import (
    MIN_EVERY_INTERVAL_SECONDS,
    ScheduleError,
    create_after_record,
    create_at_record,
    create_change,
    create_every_record,
    delete_change,
    fired_change,
    fold_schedules,
)

logger = logging.getLogger("pydsh.schedules")

#: The event a change lands as.
SCHEDULE_CHANGE_EVENT = "schedule/change"

#: Tags a delivered reminder, so a renderer does not show it as the user
#: speaking and a repeat guard does not read it as an interruption.
REMINDER_FORM = "reminder"

#: The longest a single timer is armed for. A long-dated reminder re-arms in
#: hops rather than trusting one enormous sleep, which drifts and cannot be
#: rechecked in the meantime.
MAX_TIMER_SECONDS = 300


def now_ms() -> int:
    return int(time.time() * 1000)


SCHEDULE_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": ["create", "list", "delete"],
            "description": "What to do.",
        },
        "prompt": {"type": "string", "description": "What to be reminded of."},
        "after_seconds": {"type": "integer", "description": "Remind once, after this long."},
        "at_ms": {"type": "integer", "description": "Remind once, at this instant."},
        "every_seconds": {
            "type": "integer",
            "description": f"Repeat this often (at least {MIN_EVERY_INTERVAL_SECONDS}s).",
        },
        "id": {"type": "string", "description": "Which schedule, for delete."},
    },
    "required": ["operation"],
}


class _Tool:
    def __init__(self, name: str, description: str, parameters: dict, execute) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.execute = execute


class ScheduleRuntime(Service):
    """Provides ``ctx.schedules``."""

    provide = "schedules"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._root = getattr(ctx, "root", ctx)

        tools = getattr(self._root, "tools", None)
        if tools is not None:
            dispose = tools.register(
                _Tool(
                    "schedule",
                    "Create, list, or delete a durable reminder for this session.",
                    SCHEDULE_SCHEMA,
                    self._tool,
                )
            )
            ctx.effect(lambda: dispose)
        ctx.effect(lambda: self._disarm_all)

    # -- reading ----------------------------------------------------------- #
    def _changes(self, session: Any) -> list:
        return [e.data for e in session.events if e.type == SCHEDULE_CHANGE_EVENT]

    def state(self, session: Any, at: Optional[int] = None) -> dict:
        """The active and overdue schedules, folded from the log."""
        return fold_schedules(self._changes(session), now_ms() if at is None else at)

    def list(self, session: Any) -> list[dict]:
        folded = self.state(session)
        return [*folded["overdue"], *folded["active"]]

    # -- writing ----------------------------------------------------------- #
    def create(self, session: Any, spec: dict) -> dict:
        """Record a reminder and re-arm the session's timer."""
        stamp = now_ms()
        prompt = spec.get("prompt", "")
        if spec.get("every_seconds") is not None:
            record = create_every_record(prompt, spec["every_seconds"], stamp)
        elif spec.get("after_seconds") is not None:
            record = create_after_record(prompt, spec["after_seconds"], stamp)
        elif spec.get("at_ms") is not None:
            record = create_at_record(prompt, spec["at_ms"], stamp)
        else:
            raise ScheduleError(
                "invalid_rule", "a schedule needs one of at_ms, after_seconds, or every_seconds"
            )
        session.append(SCHEDULE_CHANGE_EVENT, create_change(record))
        self.arm(session)
        return record

    def delete(self, session: Any, schedule_id: str) -> bool:
        folded = self.state(session)
        if not any(r["id"] == schedule_id for r in [*folded["active"], *folded["overdue"]]):
            return False
        session.append(SCHEDULE_CHANGE_EVENT, delete_change(schedule_id))
        self.arm(session)
        return True

    # -- the timer, a projection of the log --------------------------------- #
    def arm(self, session: Any) -> None:
        """Arm one timer for the soonest target, or disarm if there is none."""
        self._disarm(session.id)
        folded = self.state(session)
        due = folded["overdue"] or folded["active"]
        if not due:
            return

        delay = 0.0 if folded["overdue"] else max(
            0.0, (folded["active"][0]["scheduled_at"] - now_ms()) / 1000.0
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop; a later call will arm it
        handle = loop.call_later(
            min(delay, MAX_TIMER_SECONDS),
            lambda: asyncio.ensure_future(self._on_timer(session)),
        )
        self._timers[session.id] = handle

    async def _on_timer(self, session: Any) -> None:
        try:
            await self.tick(session)
        except Exception as exc:  # noqa: BLE001 - a reminder must not kill the loop
            logger.warning("schedules: delivery failed: %s", exc, exc_info=exc)
        finally:
            self.arm(session)

    async def tick(self, session: Any, at: Optional[int] = None) -> list[dict]:
        """Deliver whatever is due, re-checking the durable target (I1).

        Called by the timer, and directly by tests — which drive the clock
        rather than waiting for it.
        """
        stamp = now_ms() if at is None else at
        folded = self.state(session, stamp)
        delivered: list[dict] = []

        for record in folded["overdue"]:
            # Re-checked against the log, not trusted from the timer.
            if record["scheduled_at"] > stamp:
                continue
            late = stamp - record["scheduled_at"]
            self._deliver(session, record, late)
            next_at = (
                stamp + record["every_seconds"] * 1000
                if record["kind"] == "every"
                else None
            )
            session.append(
                SCHEDULE_CHANGE_EVENT, fired_change(record["id"], stamp, next_at)
            )
            delivered.append({**record, "late_ms": late})
        return delivered

    def _deliver(self, session: Any, record: dict, late_ms: int) -> None:
        """Inject the reminder as history, saying so if it is late."""
        text = record["prompt"]
        if late_ms > 1000:
            # Honest about session-local delivery: the reminder came due while
            # nothing was running, and the model should know that.
            text = f"{text}\n\n[this reminder was due {late_ms // 1000}s ago]"
        session.append(
            "user/message",
            encode_payload(
                create_user_message(
                    [TextBlock(text)],
                    source=MessageSource("plugin", plugin="schedule", form=REMINDER_FORM),
                )
            ),
        )

    def _disarm(self, session_id: str) -> None:
        handle = self._timers.pop(session_id, None)
        if handle is not None:
            handle.cancel()

    def _disarm_all(self) -> None:
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()

    # -- the tool ----------------------------------------------------------- #
    async def _tool(self, arguments: dict, execution: Any = None) -> str:
        agent = getattr(execution, "caller", None)
        session = getattr(agent, "session", None)
        if session is None:
            return "Error: the schedule tool needs a calling agent with a session."

        operation = arguments.get("operation")
        try:
            if operation == "create":
                record = self.create(session, arguments)
                return f"Scheduled {record['id']}: {record['prompt']}"
            if operation == "list":
                records = self.list(session)
                if not records:
                    return "No schedules."
                return "\n".join(
                    f"{r['id']}  {r['kind']:<6} {r['prompt']}" for r in records
                )
            if operation == "delete":
                removed = self.delete(session, arguments.get("id", ""))
                return "Deleted." if removed else "Error: no such schedule."
            return f"Error: unknown operation {operation!r}."
        except ScheduleError as error:
            return f"Error [{error.code}]: {error}"


__all__ = [
    "ScheduleRuntime",
    "SCHEDULE_CHANGE_EVENT",
    "REMINDER_FORM",
    "MAX_TIMER_SECONDS",
    "SCHEDULE_SCHEMA",
    "now_ms",
]
