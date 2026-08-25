"""Bounded output — keeping what matters when a tool returns too much.

Three related answers to the same problem, joined by one rule: **anything that
drops data says so**, in its own units, and never rounds "some" into silence. A
truncated result that does not admit it is worse than no result, because the
model reasons confidently from a fragment.

- :class:`ItemRetainer` / :class:`TextRetainer` bound a stream as it arrives.
- :class:`LocalSpillStore` puts the whole thing somewhere the model can read it
  back, and returns a locator.
- :class:`ToolResultPruner` cuts the middle out of a result already assembled.
"""

from .omitted import (
    assert_budget,
    describe_omitted,
    format_retention_notice,
    omitted_exact,
    omitted_none,
    omitted_unknown,
)
from .pruner import (
    DEFAULT_HEAD_CHARS,
    DEFAULT_TAIL_CHARS,
    DEFAULT_THRESHOLD_CHARS,
    PRUNE_MARKER,
    ToolResultPruner,
    resolve_config,
)
from .retention import (
    ItemRetainer,
    TextRetainer,
    trim_leading_continuation_utf8,
    trim_trailing_partial_utf8,
)
from .spill import (
    DEFAULT_ROOT_NAME,
    RETRIEVAL_HINT,
    LocalSpillStore,
    SpillStore,
    encode_segment,
    private_root,
)

__all__ = [
    # the omitted vocabulary
    "omitted_none",
    "omitted_exact",
    "omitted_unknown",
    "describe_omitted",
    "format_retention_notice",
    "assert_budget",
    # retention
    "ItemRetainer",
    "TextRetainer",
    "trim_trailing_partial_utf8",
    "trim_leading_continuation_utf8",
    # spill
    "SpillStore",
    "LocalSpillStore",
    "encode_segment",
    "private_root",
    "RETRIEVAL_HINT",
    "DEFAULT_ROOT_NAME",
    # pruning
    "ToolResultPruner",
    "resolve_config",
    "PRUNE_MARKER",
    "DEFAULT_THRESHOLD_CHARS",
    "DEFAULT_HEAD_CHARS",
    "DEFAULT_TAIL_CHARS",
]
