"""Capability seams — what the default tools are built on.

``ctx.fs``, ``ctx.shell`` and ``ctx.terminal`` provide *capability*, not
policy: they do the thing. Deciding whether a caller may do it belongs to the
tools pipeline, where a guard or an approver can see who is asking.

The exception is path containment, which lives here because a path is the
argument that escapes — and the check resolves symlinks, because a lexical one
does not hold.
"""

from .fs import (
    READ_LIMIT,
    READ_MAX_BYTES,
    READ_MAX_LINE_LENGTH,
    WRITE_INTENT,
    AmbiguousEditError,
    FileSystem,
    PathOutsideRootError,
)
from .shell import TERMINATED_EXIT_CODE, TIMEOUT_CODE, ShellService
from .terminal import (
    DEFAULT_WAIT_MS,
    TerminalClosedError,
    TerminalService,
    TerminalSession,
)
from .timeout import (
    MAX_TIMER_DELAY_MS,
    Deadline,
    IdleWatchdog,
    TimeoutReason,
    assert_timer_delay,
    clamp_timeout,
    deadline,
    timeout_of,
)

__all__ = [
    "FileSystem",
    "PathOutsideRootError",
    "AmbiguousEditError",
    "READ_LIMIT",
    "READ_MAX_LINE_LENGTH",
    "READ_MAX_BYTES",
    "WRITE_INTENT",
    "ShellService",
    "TERMINATED_EXIT_CODE",
    "TIMEOUT_CODE",
    "TerminalService",
    "TerminalSession",
    "TerminalClosedError",
    "DEFAULT_WAIT_MS",
    "TimeoutReason",
    "timeout_of",
    "Deadline",
    "deadline",
    "clamp_timeout",
    "assert_timer_delay",
    "IdleWatchdog",
    "MAX_TIMER_DELAY_MS",
]
