"""Contained event broadcast.

plugkit's ``ctx.emit`` calls each listener directly and does not catch — one
throwing observer propagates out of the dispatch and breaks whatever committed
the change. For a *post-commit notification* that is the wrong shape: the state
change already happened, so a failing observer must not rewrite history by
turning a completed append or registration into an exception.

:func:`emit_contained` is the post-commit form: every listener runs, failures
are collected and logged, and the caller proceeds. Use it for "this happened"
broadcasts. Use plain ``ctx.emit`` when a listener's failure genuinely should
abort the caller, and ``ctx.parallel`` when the caller must await the listeners
(the durability checkpoint in spec 01 is that case).
"""

from __future__ import annotations

import logging
from typing import Any

from plugkit.cordis.utils import call_listener

logger = logging.getLogger("pydsh.dispatch")


def emit_contained(ctx: Any, name: str, *args: Any) -> list[BaseException]:
    """Broadcast ``name`` post-commit; log and contain any listener failure.

    Returns the exceptions that were contained, so a caller that wants to
    assert on them in a test can, while normal callers ignore the result.
    """
    errors: list[BaseException] = []
    # plugkit has no contained dispatch mode, and its public surfaces don't fit:
    # `ctx.emit` stops at the first raising listener, `ctx.parallel` is async
    # (the callers here are synchronous commits), and the deprecated
    # `events.dispatch` binds callbacks to the carrier — which fails outright
    # when there is none. So we walk the hook list ourselves. This is the ONE
    # place that reaches into the kernel's internals; if plugkit grows a
    # contained mode, only this function body changes.
    hooks = list(ctx.events._hooks.get(name) or [])
    for hook in hooks:
        try:
            call_listener(hook["callback"], None, list(args))
        except Exception as exc:  # noqa: BLE001 - containment is the point
            errors.append(exc)
            logger.warning("listener for %r failed: %s", name, exc, exc_info=exc)
    return errors


__all__ = ["emit_contained"]
