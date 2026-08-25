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
    if ctx is None:
        # A Session rebuilt by a persistence backend has no context to
        # broadcast on. That is a valid state, not a listener failure, so it
        # is silent — logging it would fill a cold read's output with warnings
        # about something working as designed.
        return []

    events = getattr(ctx, "events", None)
    registry = getattr(events, "_hooks", None) if events is not None else None
    if registry is None:
        # A context with no kernel event system (a test stub, or a Session
        # built outside a store). There are no hook records to walk, so
        # deliver through whatever emit it does have and contain that.
        try:
            ctx.emit(name, *args)
        except Exception as exc:  # noqa: BLE001
            logger.warning("listener for %r failed: %s", name, exc, exc_info=exc)
            return [exc]
        return []

    for hook in list(registry.get(name) or []):
        try:
            call_listener(hook["callback"], None, list(args))
        except Exception as exc:  # noqa: BLE001 - containment is the point
            errors.append(exc)
            logger.warning("listener for %r failed: %s", name, exc, exc_info=exc)
    return errors


__all__ = ["emit_contained"]
