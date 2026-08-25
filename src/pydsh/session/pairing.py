"""Where the surface can safely be cut.

The constraint compaction bends around. An assistant message that requests
three tool calls is only coherent alongside the three results that answer it.
Cut between them — summarise the call but not its answer — and the model is
shown a request that was never answered. Most providers reject that outright,
and no amount of good summarising repairs it.

So: walk the surface counting outstanding tool calls. A cut where the count is
zero is **balanced** and safe. Anywhere else is not.

The walk reads the *surface*, not the log, which is the subtle part. After a
compaction a tool call may be shadowed while its result is still on the
surface; the count then goes negative, and that is a corrupt surface rather
than an unusual one — so it raises.
"""

from __future__ import annotations

import weakref
from typing import Any

from ..message.blocks import ToolCallBlock
from ..message.payload import decode_payload

#: Cached balance per session, keyed by the session object.
_CACHE: "weakref.WeakKeyDictionary[Any, dict]" = weakref.WeakKeyDictionary()


def _outstanding_delta(event: Any) -> int:
    """How this surface event changes the count of unanswered tool calls."""
    if event.type == "assistant/message":
        message = decode_payload(event.data.get("message"))
        content = getattr(message, "content", ()) or ()
        return sum(1 for block in content if isinstance(block, ToolCallBlock))
    if event.type == "tool/result":
        return -1
    return 0


def _surface_event(session: Any, seq: int) -> Any:
    """The log event a surface node points at, or a corruption error."""
    for event in session.events:
        if event.seq == seq:
            return event
    raise RuntimeError(
        f"tool-pairing: surface node {seq} has no matching log event "
        "(the surface is corrupt)"
    )


def surface_balance(session: Any) -> dict:
    """Which cuts on the current surface are balanced.

    Returns the replace generation it was computed for, a list of booleans one
    longer than the surface (the cut *before* each node, plus the cut after the
    last), and a lookup from node sequence to its index.

    Cached and invalidated by the replace generation — which exists for exactly
    this, and without which region selection would be quadratic in the surface.
    """
    nodes = session.surface_nodes
    generation = session.replace_generation
    cached = _CACHE.get(session)
    if (
        cached is not None
        and cached["generation"] == generation
        and len(cached["cut_balanced"]) == len(nodes) + 1
    ):
        return cached

    cut_balanced = [True]  # before the first node: nothing is outstanding
    index_by_seq: dict[int, int] = {}
    outstanding = 0

    by_seq = {event.seq: event for event in session.events}
    for index, seq in enumerate(nodes):
        event = by_seq.get(seq) or _surface_event(session, seq)
        outstanding += _outstanding_delta(event)
        if outstanding < 0:
            raise RuntimeError(
                f"tool-pairing: the tool result at surface node {seq} answers no "
                "call on the surface (the surface is corrupt)"
            )
        cut_balanced.append(outstanding == 0)
        index_by_seq[seq] = index

    computed = {
        "generation": generation,
        "cut_balanced": cut_balanced,
        "index_by_seq": index_by_seq,
    }
    _CACHE[session] = computed
    return computed


def _cut(session: Any, seq: int, offset: int) -> bool:
    balance = surface_balance(session)
    index = balance["index_by_seq"].get(seq)
    if index is None:
        raise RuntimeError(f"tool-pairing: surface node {seq} is not on the surface")
    return balance["cut_balanced"][index + offset]


def balanced_before(session: Any, seq: int) -> bool:
    """Whether the surface can be cut immediately before this node."""
    return _cut(session, seq, 0)


def balanced_after(session: Any, seq: int) -> bool:
    """Whether the surface can be cut immediately after this node."""
    return _cut(session, seq, 1)


__all__ = ["surface_balance", "balanced_before", "balanced_after"]
