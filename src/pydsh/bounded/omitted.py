"""What was dropped, as a value — and how to say it.

The obvious signature for "did we truncate" is a boolean, and it is wrong.
Three different facts hide behind `True`:

- 4812 items were dropped, and we counted them.
- Some were dropped and we do not know how many.
- Nothing was dropped.

A boolean collapses the first two, and the wording downstream then either
invents a precision it does not have ("thousands were omitted") or throws away
one it does ("some results were omitted"). So omission is a value with three
shapes, and the phrasing follows from which shape it is.

The split of ownership matters as much. This module writes what was *lost*; the
caller writes what to *do about it*. Only a tool knows whether the answer is
"narrow the pattern" or "read the spilled file", and a generic sentence would
be vague exactly where it needs to be specific.
"""

from __future__ import annotations

from typing import Any, Callable


def omitted_none() -> dict:
    """Nothing was dropped."""
    return {"kind": "none"}


def omitted_exact(count: int) -> dict:
    """Exactly ``count`` were dropped, and we counted them."""
    return {"kind": "exact", "count": count}


def omitted_unknown() -> dict:
    """Something was dropped and the amount is not known.

    Distinct from an exact zero *and* from an exact count: a stream that was cut
    off upstream knows it lost data without knowing how much, and pretending
    otherwise in either direction is a lie.
    """
    return {"kind": "unknown"}


def describe_omitted(omitted: dict, unit: str) -> str:
    """The standard wording for one omission, in the caller's unit.

    Renders "nothing" as the empty string, so a caller can join it
    unconditionally without checking first.
    """
    kind = omitted.get("kind")
    if kind == "none":
        return ""
    if kind == "exact":
        return f"Omitted {omitted['count']} {unit}."
    return f"More {unit} were omitted."


def format_retention_notice(
    notice: dict, recovery: Callable[[dict], str]
) -> str:
    """One footer line: the standard loss clause plus the caller's recovery one.

    :param notice: the retainer's result, plus a ``unit``. Handed whole to
        ``recovery`` so the advice can depend on what was actually kept.
    :param recovery: returns the caller's own sentence, or an empty string.
    """
    parts = [
        describe_omitted(notice.get("omitted", omitted_none()), notice.get("unit", "items")),
        recovery(notice),
    ]
    return " ".join(part for part in parts if part)


def assert_budget(value: Any, name: str) -> None:
    """A retention budget must be a non-negative integer."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {value!r}")


__all__ = [
    "omitted_none",
    "omitted_exact",
    "omitted_unknown",
    "describe_omitted",
    "format_retention_notice",
    "assert_budget",
]
