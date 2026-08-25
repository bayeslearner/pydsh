"""``ctx.long_term_memory`` — what was said in earlier conversations.

Two halves, on two seams. At ``turn/end`` the plugin pairs each prompt with its
reply and stores it, keyed by content so re-running the same exchange does not
accumulate copies. On the first step of a later turn it retrieves what overlaps
and injects it as **history**, tagged as a recall.

History, not prompt — the same rule every other context in this port follows.
A memory rewritten into the system prompt would invalidate the prompt cache
every turn and make "what was the model told" have a different answer at every
step. As history it simply sits in front of the question, the way a briefing
does, and can be compacted like anything else on the surface.

Relevance is keyword overlap with a recency fallback, and that is unglamorous
on purpose: without relevance feedback there is no way to know whether a
cleverer score is actually better, and a bad ranking that *looks* principled is
harder to diagnose than an obviously simple one. The seam is here for a
consumer that has embeddings and can measure the difference.

The plugin makes no model calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from typing import Any, Optional

from plugkit import Service

from ..message import MessageSource, TextBlock, as_text, create_user_message
from ..message.payload import decode_payload
from ..storage import define_domain, domain_table

logger = logging.getLogger("pydsh.sidecar.memory")

#: How the injected message is tagged, so a renderer can tell a recall from
#: something the user actually typed.
RECALL_FORM = "recall"

#: The plugin name on the injected message's source.
PLUGIN_NAME = "long-term-memory"

#: Characters of recalled text injected in one turn.
DEFAULT_MAX_INJECTED_CHARS = 4_000

#: Memories injected in one turn, however many overlap.
DEFAULT_MAX_RECALLED = 10

#: Memories used when nothing overlaps — the recency fallback.
DEFAULT_RECENT_COUNT = 5

#: Characters kept from each side of a captured exchange.
DEFAULT_CAPTURE_TEXT_LIMIT = 2_000

#: What the recall message says before the memories themselves.
RECALL_HEADER = (
    "Long-term memory from earlier conversations, for reference only:"
)

#: One table of memories, keyed by content digest.
MEMORY_DOMAIN = define_domain(
    "long_term_memory",
    version=1,
    tables={"entries": domain_table()},
)


def tokenize(text: str) -> list[str]:
    """Significant words: latin runs and CJK characters.

    CJK is split per character because this core ships no segmenter, and a
    per-character split still overlaps usefully on names and terms.
    """
    lower = text.lower()
    return re.findall(r"[a-z0-9]+", lower) + re.findall(r"[一-鿿]", lower)


def memory_key(text: str) -> str:
    """The content key of a memory. Identical text is one memory (R4.1)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def last_user_text(messages: Any) -> str:
    """The text of the last user message in a list — the query to recall on."""
    for message in reversed(list(messages or ())):
        if getattr(message, "role", None) == "user":
            return as_text(getattr(message, "content", ()))
    return ""


def _pair_text(user_text: str, assistant_text: str, limit: int) -> str:
    return (
        f"User: {user_text[:limit]}\nAssistant: {assistant_text[:limit]}"
    ).strip()


def _decoded(data: Any) -> Any:
    """A message payload as stored on an event, or ``None`` if unreadable."""
    if data is None:
        return None
    try:
        return decode_payload(data)
    except Exception:  # noqa: BLE001 - an unreadable payload is not a memory
        return None


def _user_text_of(data: Any) -> str:
    """A ``user/message`` event's text — only when a human said it.

    Plugin-sourced messages are skipped, and skipping them is what keeps this
    from feeding on itself: a recall injected last turn is a ``user/message``
    on the surface, so capturing it would store the memory again inside a
    memory, and every turn would compound.
    """
    message = _decoded(data)
    if getattr(getattr(message, "source", None), "kind", None) != "user":
        return ""
    return as_text(getattr(message, "content", ()) or ())


def _assistant_text_of(data: Any) -> str:
    """An ``assistant/message`` event's text (the message is nested)."""
    if not isinstance(data, dict):
        return ""
    message = _decoded(data.get("message"))
    return as_text(getattr(message, "content", ()) or ())


def build_recall(entries: list[dict], max_chars: int) -> str:
    """Render the memories into one message, within the character budget."""
    body: list[str] = []
    used = len(RECALL_HEADER)
    for entry in entries:
        line = f"- {entry['text']}"
        if used + len(line) + 1 > max_chars:
            break
        body.append(line)
        used += len(line) + 1
    return "\n".join([RECALL_HEADER, *body])


class LongTermMemory(Service):
    """Provides ``ctx.long_term_memory``."""

    provide = "long_term_memory"
    inject = ["storage_domain"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        self._max_chars = int(
            config.get("max_injected_chars", DEFAULT_MAX_INJECTED_CHARS)
        )
        self._max_recalled = int(config.get("max_recalled", DEFAULT_MAX_RECALLED))
        self._recent_count = int(config.get("recent_count", DEFAULT_RECENT_COUNT))
        self._capture_limit = int(
            config.get("capture_text_limit", DEFAULT_CAPTURE_TEXT_LIMIT)
        )
        self._capture = bool(config.get("capture", True))
        self._domain: Any = None
        self._inflight: set = set()
        ctx.on("agent/pre-step", self._recall)
        if self._capture:
            ctx.on("session/event", self._on_event)

    async def start(self) -> None:
        """Open the memory domain. Idempotent."""
        if self._domain is None:
            self._domain = await self.ctx.storage_domain.open(MEMORY_DOMAIN)

    def _table(self) -> Any:
        if self._domain is None:
            raise RuntimeError(
                "long-term memory has not been started: await "
                "ctx.long_term_memory.start() first"
            )
        return self._domain.table("entries")

    # -- reading ----------------------------------------------------------- #
    def all(self) -> list[dict]:
        """Every memory, oldest first."""
        return sorted(
            (value for _, value in self._table().entries()),
            key=lambda entry: entry["time"],
        )

    def retrieve(self, query: str) -> list[dict]:
        """The memories worth recalling for this query.

        Overlap first, most-recent as the fallback — a recall with no overlap
        returns the recent window rather than nothing, because "I have no idea
        what you mean" is rarely the truth after a long history.
        """
        stored = self.all()
        if not stored:
            return []
        words = set(tokenize(query))
        if not words:
            return stored[-self._recent_count:]
        # Scored once, not once per comparison: tokenizing inside a sort key
        # re-tokenizes every memory O(n log n) times for a result that cannot
        # change.
        matched = [
            (score, entry["time"], entry)
            for entry in stored
            if (score := len(words & set(tokenize(entry["text"])))) > 0
        ]
        if not matched:
            return stored[-self._recent_count:]
        # Highest overlap first; ties go to the more recent memory, so a repeat
        # of the same topic recalls what was most recently said about it.
        matched.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return [entry for _, _, entry in matched[: self._max_recalled]]

    # -- writing ----------------------------------------------------------- #
    async def remember(self, text: str, tags: Optional[list] = None) -> Optional[dict]:
        """Store one memory. Returns ``None`` when it is already stored."""
        await self.start()
        text = (text or "").strip()
        if not text:
            return None
        key = memory_key(text)
        if self._table().get(key) is not None:
            return None
        entry = {
            "id": key,
            "time": time.time(),
            "text": text,
            "tags": list(tags or []),
        }
        await self._table().put(key, entry)
        return entry

    async def capture(self, session: Any) -> int:
        """Store every completed exchange in a session. Returns how many were new."""
        await self.start()
        stored = 0
        for user_text, assistant_text in self._exchanges(session):
            text = _pair_text(user_text, assistant_text, self._capture_limit)
            if await self.remember(text) is not None:
                stored += 1
        return stored

    def _exchanges(self, session: Any) -> list[tuple[str, str]]:
        """Pair each human prompt with the reply that followed it.

        A prompt with no reply yet is held, not paired: capturing half an
        exchange stores a question whose answer can never be recalled with it.
        """
        pairs: list[tuple[str, str]] = []
        pending = ""
        for event in session.events:
            if event.type == "user/message":
                text = _user_text_of(event.data)
                if text:
                    pending = text
            elif event.type == "assistant/message" and pending:
                pairs.append((pending, _assistant_text_of(event.data)))
                pending = ""
        return pairs

    # -- the seams --------------------------------------------------------- #
    def _on_event(self, session: Any, event: Any) -> None:
        """Capture at the end of a turn, without making the append wait."""
        if event.type != "turn/end" or self._domain is None:
            return
        try:
            task = asyncio.ensure_future(self._capture_soft(session))
        except RuntimeError:
            return  # no running loop; the next turn picks the exchange up
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _capture_soft(self, session: Any) -> None:
        """A capture that never becomes the conversation's problem."""
        try:
            await self.capture(session)
        except Exception as exc:  # noqa: BLE001 - a lost memory is not a fault
            logger.warning("long-term memory: capture failed: %s", exc, exc_info=exc)

    async def _recall(self, payload: dict, next_: Any) -> Any:
        """Inject the relevant memories on the first step of a turn."""
        decision = await next_()
        if not isinstance(decision, dict) or decision.get("kind") != "enter":
            return decision
        if payload.get("step") != 1 or self._domain is None:
            return decision

        query = last_user_text(decision.get("messages") or payload.get("messages"))
        if not query:
            return decision
        entries = self.retrieve(query)
        if not entries:
            return decision
        text = build_recall(entries, self._max_chars)

        messages = list(decision.get("messages") or ())
        if any(
            getattr(m, "role", None) == "user" and as_text(m.content) == text
            for m in messages
        ):
            return decision  # already injected this turn

        recalled = create_user_message(
            [TextBlock(text)],
            source=MessageSource("plugin", plugin=PLUGIN_NAME, form=RECALL_FORM),
        )
        return {"kind": "enter", "messages": [recalled, *messages]}

    async def drain(self) -> None:
        """Wait for any capture in flight — for tests and for shutdown."""
        while self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)


__all__ = [
    "LongTermMemory",
    "MEMORY_DOMAIN",
    "RECALL_FORM",
    "PLUGIN_NAME",
    "RECALL_HEADER",
    "tokenize",
    "memory_key",
    "build_recall",
    "last_user_text",
    "DEFAULT_MAX_INJECTED_CHARS",
    "DEFAULT_MAX_RECALLED",
    "DEFAULT_RECENT_COUNT",
    "DEFAULT_CAPTURE_TEXT_LIMIT",
]
