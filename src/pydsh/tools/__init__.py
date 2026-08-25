"""The default tools — the behaviour that proves the seams compose.

A seam with no consumer is a claim; a seam with a working default is a
demonstration, and the piece a consumer swaps rather than writes. These are
those defaults::

    await root.plugin(FsTools)        # read / write / edit  over ctx.fs
    await root.plugin(BashTool)       # bash                 over ctx.shell
    await root.plugin(TerminalTool)   # terminal             over ctx.terminal
    await root.plugin(TodoTool)       # todo_write           owns its own state
    await root.plugin(RepeatToolGuard)
    await root.plugin(SpillPolicy)
    await root.plugin(TimeContext)

.. warning::
   ``BashTool`` and ``TerminalTool`` give a model command execution with the
   harness's privileges. Mounting them without a guard or approver on the tools
   pipeline gives the model a shell.
"""

from .context import SNAPSHOT_FORM, TIME_PREFIX, SystemInstructions, TimeContext
from .fs_tools import EDIT_SCHEMA, READ_SCHEMA, WRITE_SCHEMA, FsTools
from .guards import (
    DEFAULT_THRESHOLDS,
    GENTLE_REMINDER,
    NOTICE_FORM,
    RepeatToolGuard,
    SpillPolicy,
    canonical_arguments,
    resolve_thresholds,
)
from .shell_tools import (
    BASH_SCHEMA,
    DEFAULT_MAX_OUTPUT_BYTES,
    TERMINAL_SCHEMA,
    BashTool,
    TerminalTool,
)
from .todo_tool import (
    STATUSES,
    TODO_SCHEMA,
    TODOS_KEY,
    TODOS_PROJECTION,
    TodoError,
    TodoTool,
    to_todo_list,
)

__all__ = [
    # tools
    "FsTools",
    "BashTool",
    "TerminalTool",
    "TodoTool",
    "READ_SCHEMA",
    "WRITE_SCHEMA",
    "EDIT_SCHEMA",
    "BASH_SCHEMA",
    "TERMINAL_SCHEMA",
    "TODO_SCHEMA",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "to_todo_list",
    "TodoError",
    "TODOS_PROJECTION",
    "TODOS_KEY",
    "STATUSES",
    # guards
    "RepeatToolGuard",
    "SpillPolicy",
    "canonical_arguments",
    "resolve_thresholds",
    "GENTLE_REMINDER",
    "DEFAULT_THRESHOLDS",
    "NOTICE_FORM",
    # injectors
    "TimeContext",
    "SystemInstructions",
    "SNAPSHOT_FORM",
    "TIME_PREFIX",
]
