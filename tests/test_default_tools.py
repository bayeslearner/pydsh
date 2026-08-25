"""The default tools, through the real pipeline — Requirements 1 to 4.

Driven through `ctx.tools.execute` with real seams behind them: real files,
real subprocesses. A tool tested against a mocked seam proves the tool talks to
the mock.
"""

from __future__ import annotations

import pytest

from plugkit import Context, PointsService, ToolsService

from pydsh.capability import FileSystem, ShellService, TerminalService
from pydsh.session import SessionProjections, SessionStore
from pydsh.tools import (
    STATUSES,
    BashTool,
    FsTools,
    TerminalTool,
    TodoError,
    TodoTool,
    to_todo_list,
)

pytestmark = pytest.mark.asyncio


async def mounted(*, root_dir=None, with_shell=False, with_terminal=False,
                  with_todo=False, with_projections=False, **config) -> Context:
    ctx = Context()
    await ctx.plugin(PointsService)
    await ctx.plugin(ToolsService)
    await ctx.plugin(SessionStore)
    if with_projections:
        await ctx.plugin(SessionProjections)
    if root_dir is not None:
        await ctx.plugin(FileSystem, {"root": str(root_dir)})
        await ctx.plugin(FsTools)
    if with_shell:
        await ctx.plugin(ShellService)
        await ctx.plugin(BashTool, config.get("bash", {}))
    if with_terminal:
        await ctx.plugin(TerminalService)
        await ctx.plugin(TerminalTool)
    if with_todo:
        await ctx.plugin(TodoTool, config.get("todo", {}))
    return ctx


async def run(ctx: Context, name: str, arguments: dict, caller=None) -> str:
    result = await ctx.tools.execute(name, arguments, caller=caller)
    return result.value if result.ok else f"DENIED: {result.error}"


# --------------------------------------------------------------------------- #
# File-system tools (R1)
# --------------------------------------------------------------------------- #
async def test_read_returns_numbered_lines(tmp_path):
    (tmp_path / "f.txt").write_text("one\ntwo\nthree\n")
    ctx = await mounted(root_dir=tmp_path)

    output = await run(ctx, "read", {"path": str(tmp_path / "f.txt")})
    assert "1\tone" in output
    assert "3\tthree" in output


async def test_read_says_when_it_truncated(tmp_path):
    (tmp_path / "f.txt").write_text("\n".join(str(i) for i in range(100)))
    ctx = await mounted(root_dir=tmp_path)

    output = await run(ctx, "read", {"path": str(tmp_path / "f.txt"), "limit": 5})
    assert "truncated" in output
    assert "100 lines" in output


async def test_write_reports_what_it_wrote(tmp_path):
    ctx = await mounted(root_dir=tmp_path)
    output = await run(ctx, "write", {"path": str(tmp_path / "new.txt"), "content": "hi"})
    assert "2 bytes" in output
    assert (tmp_path / "new.txt").read_text() == "hi"


async def test_edit_replaces_exact_text(tmp_path):
    (tmp_path / "f.txt").write_text("hello world")
    ctx = await mounted(root_dir=tmp_path)

    await run(ctx, "edit", {
        "path": str(tmp_path / "f.txt"), "old_string": "world", "new_string": "there",
    })
    assert (tmp_path / "f.txt").read_text() == "hello there"


async def test_an_ambiguous_edit_comes_back_as_an_error_result(tmp_path):
    """R1.5, property 3 — with the count, so the model can widen its context."""
    (tmp_path / "f.txt").write_text("a\na\na\n")
    ctx = await mounted(root_dir=tmp_path)

    output = await run(ctx, "edit", {
        "path": str(tmp_path / "f.txt"), "old_string": "a", "new_string": "b",
    })
    assert output.startswith("Error:")
    assert "3 times" in output
    assert (tmp_path / "f.txt").read_text() == "a\na\na\n"


async def test_a_path_outside_the_root_is_an_error_result_not_an_exception(tmp_path):
    """Property 3 (I2) — the model is the caller; it gets a sentence."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("not yours")
    ctx = await mounted(root_dir=workspace)

    output = await run(ctx, "read", {"path": str(tmp_path / "secret.txt")})
    assert output.startswith("Error:")
    assert "outside the execution root" in output


async def test_reading_a_missing_file_is_an_error_result(tmp_path):
    ctx = await mounted(root_dir=tmp_path)
    output = await run(ctx, "read", {"path": str(tmp_path / "nope.txt")})
    assert output.startswith("Error:")


async def test_the_tools_are_offered_with_schemas(tmp_path):
    ctx = await mounted(root_dir=tmp_path)
    names = ctx.tools.names()
    assert {"read", "write", "edit"} <= set(names)
    assert ctx.tools.get("read").parameters["required"] == ["path"]


async def test_unmounting_removes_the_tools(tmp_path):
    """I5 — tools live and die with the plugin that registered them."""
    ctx = Context()
    await ctx.plugin(PointsService)
    await ctx.plugin(ToolsService)
    await ctx.plugin(FileSystem, {"root": str(tmp_path)})
    fiber = await ctx.plugin(FsTools)
    assert "read" in ctx.tools.names()

    fiber.dispose()
    import asyncio

    for _ in range(4):
        await asyncio.sleep(0)
    assert "read" not in ctx.tools.names()


# --------------------------------------------------------------------------- #
# The bash tool (R2)
# --------------------------------------------------------------------------- #
async def test_bash_returns_output_and_the_exit_code():
    ctx = await mounted(with_shell=True)
    output = await run(ctx, "bash", {"command": "echo hello"})
    assert "hello" in output
    assert "[exit code 0]" in output


async def test_bash_reports_a_failure_with_its_code():
    ctx = await mounted(with_shell=True)
    output = await run(ctx, "bash", {"command": "echo bad >&2; exit 3"})
    assert "[stderr]" in output
    assert "bad" in output
    assert "[exit code 3]" in output


async def test_bash_reports_a_timeout_distinguishably():
    """R2.5 — 'took too long' and 'ran and failed' call for different responses."""
    ctx = await mounted(with_shell=True)
    output = await run(ctx, "bash", {"command": "sleep 5", "timeout_ms": 150})
    assert "timed out" in output
    assert "[exit code" not in output


async def test_bash_bounds_its_output_and_says_so():
    """R2.3 (I1) — a build log is not something to paste in whole."""
    ctx = await mounted(with_shell=True, bash={"max_output_bytes": 200})
    output = await run(ctx, "bash", {"command": "for i in $(seq 1 500); do echo line $i; done"})

    assert len(output.encode()) < 4000
    assert "Omitted" in output


async def test_an_impossible_timeout_is_an_error_result():
    ctx = await mounted(with_shell=True)
    output = await run(ctx, "bash", {"command": "echo hi", "timeout_ms": 0})
    assert output.startswith("Error:")


# --------------------------------------------------------------------------- #
# The terminal tool (R3)
# --------------------------------------------------------------------------- #
async def test_the_terminal_tool_keeps_state_between_calls(tmp_path):
    """R3.2 — the whole reason it is not just repeated bash."""
    (tmp_path / "sub").mkdir()

    class Caller:
        id = "agent-1"

    ctx = await mounted(with_terminal=True)
    caller = Caller()

    await run(ctx, "terminal", {"command": f"cd {tmp_path}", "cwd": str(tmp_path)}, caller)
    await run(ctx, "terminal", {"command": "cd sub"}, caller)
    output = await run(ctx, "terminal", {"command": "pwd"}, caller)

    assert "sub" in output
    await ctx.terminal.close_all()


async def test_two_agents_get_separate_terminals(tmp_path):
    """State between calls is the feature; leaking it between agents is not."""

    class Caller:
        def __init__(self, id):
            self.id = id

    ctx = await mounted(with_terminal=True)
    first, second = Caller("a"), Caller("b")

    await run(ctx, "terminal", {"command": "MARKER=first", "cwd": str(tmp_path)}, first)
    output = await run(ctx, "terminal", {"command": "echo [$MARKER]", "cwd": str(tmp_path)}, second)

    assert "[]" in output  # the second session never saw it
    await ctx.terminal.close_all()


# --------------------------------------------------------------------------- #
# The todo tool (R4)
# --------------------------------------------------------------------------- #
async def test_a_todo_list_is_validated():
    assert to_todo_list([{"content": "a", "status": "pending"}]) == [
        {"content": "a", "status": "pending"}
    ]


async def test_empty_content_is_refused():
    with pytest.raises(TodoError, match="empty content"):
        to_todo_list([{"content": "  ", "status": "pending"}])


async def test_duplicate_content_is_refused():
    """R4.2 — two identical entries cannot be told apart afterwards."""
    with pytest.raises(TodoError, match="repeats the content"):
        to_todo_list([
            {"content": "same", "status": "pending"},
            {"content": "same", "status": "completed"},
        ])


async def test_an_unknown_status_is_refused():
    with pytest.raises(TodoError, match="expected one of"):
        to_todo_list([{"content": "a", "status": "maybe"}])
    assert set(STATUSES) == {"pending", "in_progress", "completed"}


async def test_only_one_item_may_be_in_progress():
    with pytest.raises(TodoError, match="in_progress"):
        to_todo_list([
            {"content": "a", "status": "in_progress"},
            {"content": "b", "status": "in_progress"},
        ])


async def test_parallel_progress_can_be_allowed():
    items = to_todo_list(
        [
            {"content": "a", "status": "in_progress"},
            {"content": "b", "status": "in_progress"},
        ],
        allow_parallel_in_progress=True,
    )
    assert len(items) == 2


async def test_a_todo_write_lands_on_the_session():
    ctx = await mounted(with_todo=True)
    session = ctx.sessions.create("chat-1")

    class Caller:
        pass

    caller = Caller()
    caller.session = session

    output = await run(ctx, "todo_write", {
        "items": [
            {"content": "write the spec", "status": "completed"},
            {"content": "write the code", "status": "in_progress"},
        ]
    }, caller)

    assert "2 task(s); 1 completed" in output
    assert [e.type for e in session.events] == ["todo/write"]


async def test_a_todo_write_without_an_agent_is_refused():
    """R4.5 — silence would look like success and the list would be gone."""
    ctx = await mounted(with_todo=True)
    output = await run(ctx, "todo_write", {"items": [{"content": "a", "status": "pending"}]})
    assert output.startswith("Error:")
    assert "calling agent" in output


async def test_the_todos_projection_folds_last_write_wins():
    ctx = await mounted(with_todo=True, with_projections=True)
    session = ctx.sessions.create("chat-1")

    class Caller:
        pass

    caller = Caller()
    caller.session = session

    await run(ctx, "todo_write", {"items": [{"content": "first", "status": "pending"}]}, caller)
    await run(ctx, "todo_write", {"items": [{"content": "second", "status": "completed"}]}, caller)

    todos = ctx.session_projections.snapshot(session)["values"]["todos"]
    assert todos == [{"content": "second", "status": "completed"}]
