"""The values a plugin registers, and the result of assembling them.

Three small shapes. ``text`` is either a string or a callable resolved against
the assembly context, which is what lets a section say something different on
step three than on step one without anyone re-registering it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Union

#: Static text, or a provider resolved against the assembly context.
PromptText = Union[str, Callable[[dict], str]]


@dataclass(frozen=True)
class PromptSection:
    """One ordered piece of the system prompt.

    ``complete`` marks a section that *replaces* the prompt rather than joining
    it — a mode that takes the model over entirely. At most one may be in
    effect; two is a composition error, caught at assembly.
    """

    name: str
    order: int
    text: PromptText
    complete: bool = False


@dataclass(frozen=True)
class PromptContext:
    """One dynamic runtime snapshot.

    Registered here so every contributor renders the same way, but *not* part
    of the system prompt: a snapshot of now belongs in the conversation, where
    the next turn's snapshot can supersede it.
    """

    name: str
    order: int
    text: PromptText


@dataclass
class PromptAssembly:
    """One resolved assembly, before rendering.

    Sections and contexts carry resolved text but un-interpolated variables:
    interpolation happens at render, so a waterfall listener can still rewrite
    text that contains a ``{{reference}}``.
    """

    sections: list[dict] = field(default_factory=list)
    contexts: list[dict] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)
    variables: dict[str, Optional[str]] = field(default_factory=dict)


def resolve_text(text: PromptText, context: dict) -> str:
    """A section's or context's text for this assembly."""
    return text(context) if callable(text) else text


__all__ = [
    "PromptSection",
    "PromptContext",
    "PromptAssembly",
    "PromptText",
    "resolve_text",
]
