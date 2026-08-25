"""The tools a model uses to run background work and to state its objective.

Both are thin. The jobs tools fence every call by the calling agent's session,
because ownership is the only tenancy boundary these services have. The goal
tool builds the compare-and-set bookkeeping itself, so the model supplies
*intent* — "get the tests green" — rather than a revision number it would have
to track and would eventually get wrong.
"""

from __future__ import annotations

from typing import Any

from plugkit import Service

from ..bounded import TextRetainer, format_retention_notice
from .goal_fold import GoalError
from .jobs import JobNotFound

#: Bytes of job output one read may return.
DEFAULT_MAX_OUTPUT_BYTES = 16_384
HEAD_SHARE = 0.75

JOB_START_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "The command to run in the background."},
        "cwd": {"type": "string", "description": "Where to run it."},
    },
    "required": ["command"],
}

JOB_ID_SCHEMA = {
    "type": "object",
    "properties": {"id": {"type": "string", "description": "The job id."}},
    "required": ["id"],
}

GOAL_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": ["set", "pause", "resume", "complete", "block", "clear"],
            "description": "What to do with the session's goal.",
        },
        "text": {
            "type": "string",
            "description": "The objective, for `set`. Ignored otherwise.",
        },
    },
    "required": ["operation"],
}


class _Tool:
    def __init__(self, name: str, description: str, parameters: dict, execute) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.execute = execute


def _bounded(text: str, max_bytes: int) -> str:
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    head = max(1, int(max_bytes * HEAD_SHARE))
    retainer = TextRetainer.head_tail(head, max(1, max_bytes - head))
    retainer.push(text)
    kept = retainer.finish()
    notice = format_retention_notice(
        {"omitted": kept["omitted_bytes"], "unit": "bytes"},
        lambda n: "Read again for more as the job continues.",
    )
    return f"{kept['text']}\n\n{notice}"


class JobTools(Service):
    """Registers the background-work tools."""

    provide = "job_tools"
    inject = ["tools", "jobs"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        self._max_output = int(config.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES))
        for tool in (
            _Tool("job_start", "Run a command in the background and return its id.",
                  JOB_START_SCHEMA, self._start),
            _Tool("job_list", "List the background jobs this session started.",
                  {"type": "object", "properties": {}}, self._list),
            _Tool("job_read", "Read a job's output since the last read.",
                  JOB_ID_SCHEMA, self._read),
            _Tool("job_kill", "Stop a background job.", JOB_ID_SCHEMA, self._kill),
        ):
            dispose = ctx.tools.register(tool)
            ctx.effect(lambda d=dispose: d)

    @staticmethod
    def _owner(execution: Any) -> Any:
        return getattr(execution, "caller", None)

    async def _start(self, arguments: dict, execution: Any = None) -> str:
        owner = self._owner(execution)
        if owner is None:
            return "Error: job_start needs a calling agent to own the job."
        try:
            job_id = await self.ctx.jobs.start(
                {"kind": "bash", "command": arguments.get("command", ""),
                 "cwd": arguments.get("cwd")},
                owner,
            )
        except Exception as error:  # noqa: BLE001
            return f"Error: {error}"
        return f"Started job {job_id}. Read it with job_read."

    async def _list(self, arguments: dict, execution: Any = None) -> str:
        owner = self._owner(execution)
        if owner is None:
            return "Error: job_list needs a calling agent."
        jobs = self.ctx.jobs.list(owner)
        if not jobs:
            return "No background jobs."
        return "\n".join(
            f"{j['id']}  {j['status']:<10} {j['command']}" for j in jobs
        )

    async def _read(self, arguments: dict, execution: Any = None) -> str:
        owner = self._owner(execution)
        try:
            result = self.ctx.jobs.read(arguments.get("id", ""), owner)
        except JobNotFound as error:
            return f"Error: {error}"
        output = _bounded(result["output"], self._max_output)
        return f"[{result['status']}]\n{output}" if output else f"[{result['status']}] no new output"

    async def _kill(self, arguments: dict, execution: Any = None) -> str:
        owner = self._owner(execution)
        try:
            status = await self.ctx.jobs.kill(arguments.get("id", ""), owner)
        except JobNotFound as error:
            return f"Error: {error}"
        return f"Job is {status}."


class GoalTool(Service):
    """Registers the goal tool."""

    provide = "goal_tool"
    inject = ["tools", "goals"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        dispose = ctx.tools.register(
            _Tool(
                "goal",
                "Record or update what this session is working towards. The "
                "goal survives restarts and compaction.",
                GOAL_SCHEMA,
                self._run,
            )
        )
        ctx.effect(lambda: dispose)

    async def _run(self, arguments: dict, execution: Any = None) -> str:
        agent = getattr(execution, "caller", None)
        session = getattr(agent, "session", None)
        if session is None:
            return "Error: the goal tool needs a calling agent with a session."

        operation = arguments.get("operation")
        try:
            if operation == "set":
                text = (arguments.get("text") or "").strip()
                if not text:
                    return "Error: set needs the objective as `text`."
                goal = self.ctx.goals.set(session, text)
                return f"Goal set (revision {goal['revision']}): {goal['text']}"
            if operation == "clear":
                self.ctx.goals.clear(session)
                return "Goal cleared."
            if operation in ("pause", "resume", "complete", "block"):
                goal = self.ctx.goals.transition(session, operation)
                return f"Goal is now {goal['status']} (revision {goal['revision']})."
            return f"Error: unknown operation {operation!r}."
        except GoalError as error:
            # Named specifically, because the caller is a model that has to
            # retry correctly — "stale revision" and "already active" call for
            # different next moves.
            return f"Error [{error.code}]: {error}"


__all__ = ["JobTools", "GoalTool", "JOB_START_SCHEMA", "GOAL_SCHEMA"]
