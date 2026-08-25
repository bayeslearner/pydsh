"""Cutting the middle out of a result that is already too large.

The last resort, for output that is assembled and still over budget. Keep the
head, keep the tail, replace what is between them with a marker — the head
because that is where a command says what it is doing, the tail because that is
where it says how it went.

**Deterministic on purpose.** The same content pruned twice gives the same
text. A session log is replayed, and a replay that produced different history
than the original would not be a replay.

**Verified on purpose.** A prune whose output is not smaller than its input is
a bug that would otherwise pass silently: the budget appears to have been
applied and nothing was actually saved. So the result is checked and the failure
raises.
"""

from __future__ import annotations

from typing import Any, Optional

from plugkit import Service

from ..message.blocks import TextBlock

#: What replaces the removed middle. Written for the model to read, so it knows
#: the gap is deliberate rather than the output ending strangely.
PRUNE_MARKER = "\n\n[... tool result middle pruned ...]\n\n"

#: Characters a result may reach before it is pruned.
DEFAULT_THRESHOLD_CHARS = 8192

#: Characters kept from the start — where a command states its intent.
DEFAULT_HEAD_CHARS = 4096

#: Characters kept from the end — where a command states its outcome.
DEFAULT_TAIL_CHARS = 1024

_CONFIG_KEYS = frozenset({"threshold_chars", "head_chars", "tail_chars"})


def resolve_config(config: Optional[dict] = None) -> dict:
    """Validate the budgets, rejecting a configuration that cannot work."""
    config = config or {}
    unknown = set(config) - _CONFIG_KEYS
    if unknown:
        raise ValueError(
            f"tool-result pruner: unknown config key(s) {', '.join(sorted(unknown))} "
            f"(allowed: {', '.join(sorted(_CONFIG_KEYS))})"
        )

    resolved = {
        "threshold_chars": config.get("threshold_chars", DEFAULT_THRESHOLD_CHARS),
        "head_chars": config.get("head_chars", DEFAULT_HEAD_CHARS),
        "tail_chars": config.get("tail_chars", DEFAULT_TAIL_CHARS),
    }
    threshold = resolved["threshold_chars"]
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold <= 0:
        raise ValueError(f"tool-result pruner: threshold_chars must be positive, got {threshold!r}")
    for name in ("head_chars", "tail_chars"):
        value = resolved[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                f"tool-result pruner: {name} must be a non-negative integer, got {value!r}"
            )

    emitted = resolved["head_chars"] + len(PRUNE_MARKER) + resolved["tail_chars"]
    if emitted > threshold:
        # Otherwise pruning would produce something over the threshold, and the
        # verify step would reject every prune the config ever asked for.
        raise ValueError(
            f"tool-result pruner: head + marker + tail ({emitted}) must not exceed "
            f"threshold_chars ({threshold})"
        )
    return resolved


class ToolResultPruner(Service):
    """Provides ``ctx.tool_result_pruner``."""

    provide = "tool_result_pruner"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self.config = resolve_config(config)

    def measure_content(self, blocks: list) -> int:
        """Characters across the text blocks. Non-text blocks count as nothing.

        An image block has a cost, but not one measured in characters, and
        guessing an equivalence here would make the budget mean two things.
        """
        return sum(len(b.text) for b in blocks if isinstance(b, TextBlock))

    def prune_content(self, blocks: list) -> Optional[list]:
        """Replace the middle of over-budget content; ``None`` when it fits.

        Non-text blocks keep their position: the sequence a model reads is part
        of the meaning, and dropping an image because the text around it was
        long would change what the result says.
        """
        total = self.measure_content(blocks)
        if total <= self.config["threshold_chars"]:
            return None

        removed_from = self.config["head_chars"]
        removed_to = total - self.config["tail_chars"]

        pruned: list = []
        consumed = 0
        marked = False

        for block in blocks:
            if not isinstance(block, TextBlock):
                pruned.append(block)
                continue

            characters = block.text
            start = consumed
            end = start + len(characters)
            consumed = end

            head_end = min(len(characters), max(0, removed_from - start))
            tail_start = min(len(characters), max(0, removed_to - start))
            overlaps = start < removed_to and end > removed_from

            marker = ""
            if overlaps and not marked:
                marker = PRUNE_MARKER
                marked = True

            text = characters[:head_end] + marker + characters[tail_start:]
            if text:
                pruned.append(TextBlock(text))

        if not marked:
            raise RuntimeError(
                "tool-result pruner: the removed range matched no text block, so "
                "nothing was pruned — the measurement and the walk disagree"
            )

        after = self.measure_content(pruned)
        if after >= total or after > self.config["threshold_chars"]:
            # Passing this on would mean the budget silently did nothing.
            raise RuntimeError(
                f"tool-result pruner: pruning produced {after} characters from "
                f"{total} against a threshold of {self.config['threshold_chars']}; "
                "a prune must shrink and must fit"
            )
        return pruned


__all__ = [
    "ToolResultPruner",
    "resolve_config",
    "PRUNE_MARKER",
    "DEFAULT_THRESHOLD_CHARS",
    "DEFAULT_HEAD_CHARS",
    "DEFAULT_TAIL_CHARS",
]
