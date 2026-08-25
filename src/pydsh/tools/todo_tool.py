"""``todo_write`` — the one default tool that owns its own state.

Every other tool here fronts a seam. This one is a product: an agent session
owns a task list, and there is no swappable provider behind it.

Whole-list writes only. The model sends the complete list every time and it
replaces the last one — no partial updates, no per-item edits. That is a
deliberate narrowing: a model that can edit item three will eventually edit
item three of a list it has misremembered, and last-write-wins over a full list
has no such failure. The cost is a few more tokens per call.
"""

from __future__ import annotations

from typing import Any, Optional

from plugkit import Service

from ..session.projection import ProjectionDefinition

#: The states a task can be in.
STATUSES = ("pending", "in_progress", "completed")

#: The key the todos projection owns.
TODOS_KEY = "todos"

TODO_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "description": "The complete list. Replaces the previous one entirely.",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "What the task is."},
                    "status": {
                        "type": "string",
                        "enum": list(STATUSES),
                        "description": "Where the task has got to.",
                    },
                },
                "required": ["content", "status"],
            },
        }
    },
    "required": ["items"],
}


class TodoError(ValueError):
    """A list the model sent that cannot be stored as given."""


def to_todo_list(items: Any, allow_parallel_in_progress: bool = False) -> list[dict]:
    """Validate a whole list, tightening what the schema cannot express.

    The schema can say "a string" and "one of these statuses". It cannot say
    "non-empty", "unique", or "at most one in progress", and those are the
    constraints that make a list usable rather than merely well-formed.
    """
    if not isinstance(items, list):
        raise TodoError("items must be a list")

    seen: set[str] = set()
    result: list[dict] = []
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise TodoError(f"item {index} is not an object")
        content = str(raw.get("content", "")).strip()
        status = raw.get("status")
        if not content:
            raise TodoError(f"item {index} has empty content")
        if content in seen:
            # Two identical entries cannot be told apart afterwards — the model
            # would be describing one task twice and unable to update either.
            raise TodoError(f"item {index} repeats the content {content!r}")
        if status not in STATUSES:
            raise TodoError(
                f"item {index} has status {status!r}; expected one of {', '.join(STATUSES)}"
            )
        seen.add(content)
        result.append({"content": content, "status": status})

    if not allow_parallel_in_progress:
        in_progress = [item for item in result if item["status"] == "in_progress"]
        if len(in_progress) > 1:
            raise TodoError(
                f"{len(in_progress)} items are in_progress; only one may be unless "
                "the deployment allows parallel work"
            )
    return result


def _apply(state: dict, event: Any) -> dict:
    if event.type != "todo/write":
        return state
    return {"items": list(event.data.get("items", []))}


#: Last-write-wins over the whole list, which is exactly what the tool does.
TODOS_PROJECTION = ProjectionDefinition(
    key=TODOS_KEY,
    init=lambda: {"items": []},
    apply=_apply,
    view=lambda state: list(state["items"]),
)


class _Tool:
    def __init__(self, name: str, description: str, parameters: dict, execute) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.execute = execute


class TodoTool(Service):
    """Registers ``todo_write``, and the todos projection when one is mounted."""

    provide = "todo_tool"
    inject = ["tools"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        self._allow_parallel = bool(config.get("allow_parallel_in_progress", False))
        self._root = getattr(ctx, "root", ctx)

        dispose = ctx.tools.register(
            _Tool(
                "todo_write",
                "Record the complete task list, replacing the previous one.",
                TODO_SCHEMA,
                self._write,
            )
        )
        ctx.effect(lambda: dispose)

        projections = getattr(self._root, "session_projections", None)
        if projections is not None:
            release = projections.register(TODOS_PROJECTION)
            ctx.effect(lambda: release)

    async def _write(self, arguments: dict, execution: Any = None) -> str:
        agent = getattr(execution, "caller", None)
        session = getattr(agent, "session", None)
        if session is None:
            # Refused rather than quietly doing nothing: a caller with no
            # session has nowhere to write, and silence would look like success.
            return (
                "Error: todo_write needs a calling agent — there is no session "
                "to record the list in."
            )

        try:
            items = to_todo_list(arguments.get("items"), self._allow_parallel)
        except TodoError as error:
            return f"Error: {error}"

        session.append("todo/write", {"items": items})
        done = sum(1 for item in items if item["status"] == "completed")
        return f"Recorded {len(items)} task(s); {done} completed."


__all__ = [
    "TodoTool",
    "to_todo_list",
    "TodoError",
    "TODO_SCHEMA",
    "TODOS_PROJECTION",
    "TODOS_KEY",
    "STATUSES",
]
