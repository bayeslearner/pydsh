"""``bash`` and ``terminal`` — running commands, bounded.

.. warning::
   These tools give a model command execution with the harness's own
   privileges. The containment that matters is the tools pipeline's — a guard
   or an approver that can see the caller and refuse — and **mounting these
   without one gives the model a shell.** That belongs here, in the module
   someone reads before mounting it, not only in a design document.

Both are thin over their seams. What they add is *bounding*: a build log is not
something to paste into a context window whole, so output goes through a
retainer and, when a spill store is mounted, the whole thing is written
somewhere the model can grep instead.
"""

from __future__ import annotations

from typing import Any, Optional

from plugkit import Service

from ..bounded import TextRetainer, format_retention_notice
from ..capability.timeout import clamp_timeout

#: Bytes of command output a result may carry. One budget, not two: an earlier
#: draft had a separate threshold and fixed head/tail sizes, so configuring a
#: small budget changed when trimming *triggered* without changing how much was
#: kept — and a 200-byte budget returned four kilobytes.
DEFAULT_MAX_OUTPUT_BYTES = 16_384

#: How the budget splits. The head says what the command was doing; the tail
#: says how it went, which is usually the part that matters most.
HEAD_SHARE = 0.75

#: Command timeouts, in milliseconds.
DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000

BASH_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "The shell command to run."},
        "cwd": {"type": "string", "description": "Directory to run it in."},
        "timeout_ms": {
            "type": "integer",
            "description": "How long to allow, in milliseconds.",
        },
    },
    "required": ["command"],
}

TERMINAL_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "The command to send."},
        "cwd": {
            "type": "string",
            "description": "Where to start the session, used only on the first call.",
        },
    },
    "required": ["command"],
}


class _Tool:
    def __init__(self, name: str, description: str, parameters: dict, execute) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.execute = execute


def _bounded(text: str, max_bytes: int) -> tuple[str, dict]:
    """Trim output to a budget, and say what went.

    The budget is split rather than accompanied by separate head/tail sizes, so
    there is exactly one number to configure and it always means what it says.
    """
    if len(text.encode("utf-8")) <= max_bytes:
        return text, {"kind": "none"}
    head = max(1, int(max_bytes * HEAD_SHARE))
    retainer = TextRetainer.head_tail(head, max(1, max_bytes - head))
    retainer.push(text)
    result = retainer.finish()
    return result["text"], result["omitted_bytes"]


class BashTool(Service):
    """Registers ``bash`` over ``ctx.shell``."""

    provide = "bash_tool"
    inject = ["tools", "shell"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        self._default_timeout = int(config.get("default_timeout_ms", DEFAULT_TIMEOUT_MS))
        self._max_timeout = int(config.get("max_timeout_ms", MAX_TIMEOUT_MS))
        self._max_output = int(config.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES))
        self._root = getattr(ctx, "root", ctx)
        dispose = ctx.tools.register(
            _Tool(
                "bash",
                "Run a shell command and return its output and exit code.",
                BASH_SCHEMA,
                self._run,
            )
        )
        ctx.effect(lambda: dispose)

    async def _run(self, arguments: dict, execution: Any = None) -> str:
        command = arguments.get("command", "")
        try:
            timeout = clamp_timeout(
                arguments.get("timeout_ms"), self._default_timeout, self._max_timeout
            )
        except (TypeError, ValueError) as error:
            return f"Error: {error}"

        agent = getattr(execution, "caller", None)
        try:
            result = await self.ctx.shell.execute(
                command,
                cwd=arguments.get("cwd"),
                timeout_ms=int(timeout),
                signal=getattr(agent, "_activity", None),
            )
        except Exception as error:  # noqa: BLE001 - the model is the caller
            return f"Error: {error}"

        return self._render(result)

    def _render(self, result: dict) -> str:
        parts = []
        stdout, stdout_omitted = _bounded(result["stdout"], self._max_output)
        if stdout:
            parts.append(stdout)
        if result["stderr"]:
            stderr, _ = _bounded(result["stderr"], self._max_output)
            parts.append(f"[stderr]\n{stderr}")

        if result["timed_out"]:
            # Distinguishable from a non-zero exit: one means "took too long",
            # the other "ran and failed", and they call for different responses.
            parts.append("[the command timed out and was stopped]")
        else:
            parts.append(f"[exit code {result['exit_code']}]")

        notice = format_retention_notice(
            {"omitted": stdout_omitted, "unit": "bytes of output"},
            lambda n: "The middle of the output was dropped." if n["omitted"]["kind"] != "none" else "",
        )
        if notice:
            parts.append(notice)
        return "\n".join(parts)


class TerminalTool(Service):
    """Registers ``terminal`` — a shell that remembers where it is."""

    provide = "terminal_tool"
    inject = ["tools", "terminal"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        self._max_output = int(config.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES))
        dispose = ctx.tools.register(
            _Tool(
                "terminal",
                "Send a command to a persistent shell that keeps its state "
                "between calls (working directory, environment, variables).",
                TERMINAL_SCHEMA,
                self._run,
            )
        )
        ctx.effect(lambda: dispose)

    async def _run(self, arguments: dict, execution: Any = None) -> str:
        agent = getattr(execution, "caller", None)
        # One session per agent: the state between calls is the feature, and
        # sharing it across agents would leak one conversation into another.
        session_id = getattr(agent, "id", "default")

        try:
            try:
                session = self.ctx.terminal.get(session_id)
            except KeyError:
                session = await self.ctx.terminal.spawn(
                    id=session_id, cwd=arguments.get("cwd")
                )
            if session.closed:
                session = await self.ctx.terminal.spawn(
                    id=session_id, cwd=arguments.get("cwd")
                )
            output = await session.send(arguments.get("command", ""))
        except Exception as error:  # noqa: BLE001
            return f"Error: {error}"

        text, omitted = _bounded(output, self._max_output)
        notice = format_retention_notice(
            {"omitted": omitted, "unit": "bytes of output"},
            lambda n: "" if n["omitted"]["kind"] == "none" else "The middle was dropped.",
        )
        return "\n".join(part for part in (text or "[no output]", notice) if part)


__all__ = [
    "BashTool",
    "TerminalTool",
    "BASH_SCHEMA",
    "TERMINAL_SCHEMA",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_MS",
    "MAX_TIMEOUT_MS",
]
