"""The OpenAI-compatible `/chat/completions` dialect.

One translation in each direction, with a transport in between::

    messages → serialize → wire body → transport → SSE → translate → StreamChunk

Two things here are deliberately not what the reference does, and both are the
kind of bug that only shows up in the ordinary case.

**Block indices are allocated here.** The wire numbers its *tool calls* from
zero, in a namespace of its own. Using that number as the harness block index —
as the reference does — means a response with a paragraph of text and one tool
call produces two different blocks both claiming index 0, and nothing
downstream can tell them apart.

**Tool results are serialized before user text.** An endpoint requires the
`role: "tool"` messages answering a call to follow that call with nothing
between. A harness user message can carry both results and text, and emitting
the text first puts a user message in exactly that gap.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterable, AsyncIterator, Awaitable, Callable, Optional

from plugkit import Service

from ...message import (
    Message,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    as_text,
)
from ..adapter import LlmAdapter, LlmProviderInfo
from ..chunks import ChunkType, GenerateOptions, StreamChunk
from ..errors import LlmError, normalize_api_key
from .transport import Transport, aborted, resolve_transport

#: The sentinel that ends an SSE stream. Everything is held until it arrives.
DONE = "[DONE]"

#: The SSE field carrying a payload. Other fields (`event:`, comments) are not
#: part of this dialect and are skipped.
DATA_PREFIX = "data:"

#: What a tool result with no output sends. Never an empty string: some
#: endpoints reject one, and a blank result is indistinguishable from a tool
#: that ran and said nothing.
NO_OUTPUT = "(no output)"

#: The path appended to a provider's base URL.
COMPLETIONS_PATH = "/chat/completions"

#: Sent when a provider allows an empty key, so the header is well-formed.
PLACEHOLDER_KEY = "not-needed"


# --------------------------------------------------------------------------- #
# Serialization — harness messages to the wire
# --------------------------------------------------------------------------- #
def _blocks_of(message: Any) -> tuple:
    content = message.content if isinstance(message, Message) else message.get("content", ())
    if isinstance(content, str):
        return (TextBlock(content),)
    return tuple(content or ())


def _role_of(message: Any) -> str:
    return message.role if isinstance(message, Message) else message.get("role", "user")


def serialize_messages(messages: Any) -> list[dict]:
    """Translate harness messages into wire messages.

    Tool results become their own ``role: "tool"`` messages and are emitted
    **before** any text from the same harness message (I2) — the endpoint needs
    them adjacent to the assistant turn that asked for them.
    """
    wire: list[dict] = []
    for message in messages or ():
        role = _role_of(message)
        blocks = _blocks_of(message)

        if role == "system":
            wire.append({"role": "system", "content": as_text(blocks)})
            continue

        if role == "assistant":
            tool_calls = [
                {
                    "id": b.id,
                    "type": "function",
                    "function": {"name": b.name, "arguments": b.arguments},
                }
                for b in blocks
                if isinstance(b, ToolCallBlock)
            ]
            out: dict[str, Any] = {"role": "assistant", "content": as_text(blocks)}
            if tool_calls:
                reasoning = "".join(
                    b.text for b in blocks if isinstance(b, ReasoningBlock)
                )
                if reasoning:
                    # Sent back only alongside tool calls, which is the thinking
                    # -mode convention: reasoning replayed without the call it
                    # led to is context the model cannot act on.
                    out["reasoning_content"] = reasoning
                out["tool_calls"] = tool_calls
            wire.append(out)
            continue

        results = [b for b in blocks if isinstance(b, ToolResultBlock)]
        for result in results:
            wire.append(
                {
                    "role": "tool",
                    "tool_call_id": result.tool_call_id,
                    "content": as_text(result.content) or NO_OUTPUT,
                }
            )
        text = as_text(blocks)
        if text or not results:
            # `content` is never null: a user message with nothing in it is an
            # empty string, which is a different thing from an absent field.
            wire.append({"role": "user", "content": text})
    return wire


def serialize_request(options: GenerateOptions) -> dict:
    """The full wire body. Always streaming, always asking for usage."""
    messages: list[dict] = []
    if options.system:
        messages.append({"role": "system", "content": options.system})
    messages.extend(serialize_messages(options.messages))

    body: dict[str, Any] = {
        "model": options.model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if options.tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {}),
                },
            }
            for tool in options.tools
        ]
    if options.temperature is not None:
        body["temperature"] = options.temperature
    if options.max_tokens is not None:
        body["max_tokens"] = options.max_tokens
    if options.stop:
        body["stop"] = list(options.stop)
    if options.reasoning_effort:
        # Carried rather than dropped: the seam resolves it through the call
        # config, and an adapter that silently discards it makes a configured
        # setting look like it had no effect.
        body["reasoning_effort"] = options.reasoning_effort
    return body


# --------------------------------------------------------------------------- #
# Translation — the wire to harness chunks
# --------------------------------------------------------------------------- #
#: The wire's finish reasons that map cleanly onto a harness finish.
FINISH_REASONS = {
    "stop": {"kind": "stop"},
    "tool_calls": {"kind": "tool-calls"},
    "length": {"kind": "max-tokens"},
}


def map_finish_reason(reason: str) -> dict:
    """A wire finish reason as a harness one; anything unknown is an error."""
    mapped = FINISH_REASONS.get(reason)
    if mapped is not None:
        return dict(mapped)
    # `content_filter` and any future value land here. An unrecognised stop is
    # not a clean stop: the answer was cut off for a reason the caller has to
    # be told about.
    return {
        "kind": "error",
        "failure": {
            "message": f"the model stopped: {reason}",
            "code": str(reason).upper(),
        },
    }


def map_usage(usage: dict) -> dict:
    """Wire usage as harness usage, with the counts kept disjoint.

    Cache reads are deducted from input tokens because the wire reports them
    *inside* `prompt_tokens`. Left in, a caller adding the fields up counts the
    cached prefix twice.
    """
    details = usage.get("prompt_tokens_details") or {}
    cache_read = details.get("cached_tokens")
    if cache_read is None:
        cache_read = usage.get("prompt_cache_hit_tokens")
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")

    mapped: dict[str, Any] = {
        "input_tokens": (usage.get("prompt_tokens") or 0) - (cache_read or 0),
        "output_tokens": usage.get("completion_tokens") or 0,
    }
    if cache_read is not None:
        mapped["cache_read_tokens"] = cache_read
    if reasoning is not None:
        mapped["reasoning_tokens"] = reasoning
    return mapped


class _Blocks:
    """The open blocks of one response, and the indices they were given.

    The index is a *harness* identity allocated here. The wire's
    ``tool_calls[].index`` numbers the provider's own tool calls and starts at
    zero however much text came first, so it cannot be used as one.
    """

    def __init__(self) -> None:
        self._next = 0
        self.order: list[tuple[str, int]] = []
        self.text: Optional[dict] = None
        self.reasoning: Optional[dict] = None
        self.tools: dict[int, dict] = {}
        self._wire_to_index: dict[Any, int] = {}

    def allocate(self, kind: str) -> int:
        index = self._next
        self._next += 1
        self.order.append((kind, index))
        return index

    def index_for_wire(self, wire_index: Any) -> tuple[int, bool]:
        """The harness index for a wire tool-call index, and whether it is new."""
        existing = self._wire_to_index.get(wire_index)
        if existing is not None:
            return existing, False
        index = self.allocate("tool")
        self._wire_to_index[wire_index] = index
        return index, True

    @property
    def empty(self) -> bool:
        return not self.order


async def translate(payloads: AsyncIterable[str]) -> AsyncIterator[StreamChunk]:
    """Turn SSE payloads into harness chunks.

    Block ends, usage and the finish are all held until ``[DONE]``, so a
    consumer sees one coherent tail rather than a finish that may be followed
    by more content.

    :raises LlmError: a malformed payload, or a stream that ended early.
    """
    blocks = _Blocks()
    pending_finish: Optional[dict] = None
    pending_usage: Optional[dict] = None

    async for payload in payloads:
        if payload == DONE:
            for kind, index in blocks.order:
                if kind == "text":
                    yield StreamChunk(
                        ChunkType.BLOCK_END,
                        index=index,
                        block=TextBlock(blocks.text["text"]),
                    )
                elif kind == "reasoning":
                    yield StreamChunk(
                        ChunkType.BLOCK_END,
                        index=index,
                        block=ReasoningBlock(text=blocks.reasoning["text"]),
                    )
                else:
                    call = blocks.tools[index]
                    yield StreamChunk(
                        ChunkType.BLOCK_END,
                        index=index,
                        block=ToolCallBlock(
                            id=call["id"],
                            name=call["name"] or "",
                            arguments=call["arguments"],
                        ),
                    )
            if pending_usage is not None:
                yield StreamChunk(ChunkType.USAGE, usage=pending_usage)

            finish = pending_finish or {"kind": "stop"}
            if finish.get("kind") == "stop" and blocks.empty:
                # A clean stop with nothing in it is not a valid answer, and
                # passing it on as one gives the loop an empty turn to record
                # as a success.
                finish = {
                    "kind": "error",
                    "failure": {
                        "message": "the model returned a completed response with no content",
                        "code": "EMPTY_RESPONSE",
                    },
                }
            yield StreamChunk(ChunkType.FINISH, finish=finish)
            return

        try:
            frame = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise LlmError(
                f"malformed SSE payload: {payload[:120]}", "MALFORMED_RESPONSE"
            ) from exc

        for choice in frame.get("choices") or ():
            delta = choice.get("delta") or {}

            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                if blocks.reasoning is None:
                    index = blocks.allocate("reasoning")
                    blocks.reasoning = {"index": index, "text": ""}
                    yield StreamChunk(
                        ChunkType.BLOCK_START, index=index, block_type="reasoning"
                    )
                blocks.reasoning["text"] += reasoning
                yield StreamChunk(
                    ChunkType.REASONING_DELTA,
                    index=blocks.reasoning["index"],
                    reasoning=reasoning,
                )

            content = delta.get("content")
            if isinstance(content, str) and content:
                if blocks.text is None:
                    index = blocks.allocate("text")
                    blocks.text = {"index": index, "text": ""}
                    yield StreamChunk(
                        ChunkType.BLOCK_START, index=index, block_type="text"
                    )
                blocks.text["text"] += content
                yield StreamChunk(
                    ChunkType.TEXT_DELTA, index=blocks.text["index"], text=content
                )

            for call in delta.get("tool_calls") or ():
                index, is_new = blocks.index_for_wire(call.get("index", 0))
                if is_new:
                    blocks.tools[index] = {"id": "", "name": None, "arguments": ""}
                    yield StreamChunk(
                        ChunkType.BLOCK_START, index=index, block_type="tool-call"
                    )
                state = blocks.tools[index]
                if call.get("id") is not None:
                    state["id"] = call["id"]
                function = call.get("function") or {}
                if function.get("name") is not None:
                    state["name"] = function["name"]
                fragment = function.get("arguments") or ""
                state["arguments"] += fragment
                yield StreamChunk(
                    ChunkType.TOOL_CALL_DELTA,
                    index=index,
                    tool_call_id=state["id"],
                    tool_call_name=state["name"],
                    arguments_delta=fragment,
                )

            reason = choice.get("finish_reason")
            if isinstance(reason, str):
                pending_finish = map_finish_reason(reason)

        if frame.get("usage"):
            pending_usage = map_usage(frame["usage"])

    # The stream ended without `[DONE]`. The answer is truncated, and a clean
    # finish here would hand the caller half an answer as a whole one.
    raise LlmError(
        "the SSE stream ended before [DONE]; the response is truncated",
        "STREAM_CLOSED",
    )


# --------------------------------------------------------------------------- #
# The adapter
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ProviderConfig:
    """How to reach one OpenAI-compatible provider."""

    provider: str
    display_name: str
    base_url: str
    #: The *name* of the credential, never the credential. Resolved per call.
    api_key_ref: str = ""
    allow_empty_key: bool = False
    context_window: Optional[int] = None
    max_tokens: Optional[int] = None


class OpenAICompatibleAdapter(LlmAdapter):
    """Talks to any OpenAI-compatible ``/chat/completions`` endpoint.

    Endpoint and credential resolution are injected, so the registering plugin
    owns the policy — which providers exist, where their keys live, whether an
    empty one is allowed — and this class owns only the dialect.
    """

    def __init__(
        self,
        resolve_endpoint: Callable[[str], ProviderConfig],
        resolve_api_key: Callable[[str], Awaitable[str]],
        transport: Optional[Transport] = None,
    ) -> None:
        self._resolve_endpoint = resolve_endpoint
        self._resolve_api_key = resolve_api_key
        self._transport = resolve_transport(transport)

    def provider_info(self, provider: str) -> LlmProviderInfo:
        try:
            endpoint = self._resolve_endpoint(provider)
        except LlmError:
            return LlmProviderInfo(id=provider, name=provider)
        return LlmProviderInfo(id=provider, name=endpoint.display_name)

    async def resolve_model(self, provider: str, model: str) -> dict:
        """Model metadata, carrying the provider's context window when known.

        Compaction budgets against ``context_window``; without it the engine
        has to fall back on a default that may be wrong by an order of
        magnitude in either direction.
        """
        info: dict[str, Any] = {"provider": provider, "id": model, "name": model}
        try:
            endpoint = self._resolve_endpoint(provider)
        except LlmError:
            return info
        context: dict[str, Any] = {}
        if endpoint.context_window is not None:
            context["context_window"] = endpoint.context_window
        if endpoint.max_tokens is not None:
            context["max_tokens"] = endpoint.max_tokens
        if context:
            info["context"] = context
        return info

    async def _authorization(self, provider: str, endpoint: ProviderConfig) -> str:
        """The bearer header value, or a refusal — before any request is made.

        Validated locally so a missing key is reported as a missing key. A
        remote 401 tells someone their key is wrong when in fact they never set
        one, which sends them looking in the wrong place.
        """
        key = self._resolve_api_key(provider)
        if hasattr(key, "__await__"):
            key = await key
        verdict, normalized = normalize_api_key(key or "")
        if verdict == "empty":
            if not endpoint.allow_empty_key:
                raise LlmError(
                    f"provider {provider!r} has no API key; set the credential "
                    f"{endpoint.api_key_ref!r}",
                    "MISSING_CREDENTIAL",
                )
            normalized = ""
        elif verdict == "illegal":
            # The key itself is never echoed — `normalize_api_key` refuses to
            # return it — so a malformed secret cannot reach a log this way.
            raise LlmError(
                f"the API key for provider {provider!r} contains characters that "
                "cannot go in a header (printable ASCII only)",
                "ILLEGAL_API_KEY",
            )
        return f"Bearer {normalized or PLACEHOLDER_KEY}"

    def serialize(self, options: GenerateOptions) -> dict:
        """The wire body for one call. A dialect overrides this, not `stream`."""
        return serialize_request(options)

    def wrap_lines(self, lines: Any, options: GenerateOptions) -> Any:
        """A hook for a dialect that bounds or watches the raw line stream.

        Identity here. It exists so a subclass adding, say, an idle timeout does
        not have to reimplement `stream` — and so the two cannot drift.
        """
        return lines

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        endpoint = self._resolve_endpoint(options.provider)
        headers = {
            "authorization": await self._authorization(options.provider, endpoint),
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        body = self.serialize(options)
        url = f"{endpoint.base_url.rstrip('/')}{COMPLETIONS_PATH}"

        async def payloads() -> AsyncIterator[str]:
            lines = self._transport(url, body, headers, options.signal)
            if hasattr(lines, "__await__"):
                lines = await lines
            async for line in self.wrap_lines(lines, options):
                if aborted(options.signal):
                    return
                line = line.strip()
                if not line.startswith(DATA_PREFIX):
                    # Comments, `event:` fields and blank keep-alives. Not part
                    # of this dialect, and skipping them is not data loss.
                    continue
                yield line[len(DATA_PREFIX):].strip()

        async for chunk in translate(payloads()):
            yield chunk


# --------------------------------------------------------------------------- #
# The provider table and the plugin
# --------------------------------------------------------------------------- #
#: The seven vendors that speak this dialect, registered dormant: routable, and
#: unusable until their credential resolves. Base URLs are constants here and
#: overridable by config — the reference reads two of them from the environment
#: at *import* time, which freezes them for the process and makes them
#: untestable.
DEFAULT_PROVIDERS: tuple[ProviderConfig, ...] = (
    ProviderConfig("openai", "OpenAI", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    ProviderConfig(
        "qwen",
        "Qwen",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY",
    ),
    ProviderConfig("zhipu", "Zhipu GLM", "https://open.bigmodel.cn/api/paas/v4", "ZHIPU_API_KEY"),
    ProviderConfig("moonshot", "Moonshot Kimi", "https://api.moonshot.cn/v1", "MOONSHOT_API_KEY"),
    ProviderConfig("deepseek", "DeepSeek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    ProviderConfig(
        "ollama", "Ollama", "http://localhost:11434/v1", "OLLAMA_API_KEY",
        allow_empty_key=True,
    ),
    ProviderConfig(
        "vllm", "vLLM", "http://localhost:8000/v1", "VLLM_API_KEY",
        allow_empty_key=True,
    ),
)


def merge_providers(defaults: Any, overrides: Any) -> list[ProviderConfig]:
    """The provider table, with config overriding defaults by name."""
    table = {config.provider: config for config in defaults}
    for raw in overrides or ():
        name = raw.get("provider")
        if not name:
            raise LlmError(
                "a provider override needs a `provider` name", "INVALID_REQUEST"
            )
        existing = table.get(name)
        base_url = raw.get("base_url") or (existing.base_url if existing else None)
        if not base_url:
            raise LlmError(
                f"provider {name!r} has no base_url and is not a known default",
                "INVALID_REQUEST",
            )
        table[name] = ProviderConfig(
            provider=name,
            display_name=raw.get(
                "display_name", existing.display_name if existing else name
            ),
            base_url=base_url,
            api_key_ref=raw.get(
                "api_key_ref", existing.api_key_ref if existing else ""
            ),
            allow_empty_key=bool(
                raw.get(
                    "allow_empty_key", existing.allow_empty_key if existing else False
                )
            ),
            context_window=raw.get(
                "context_window", existing.context_window if existing else None
            ),
            max_tokens=raw.get("max_tokens", existing.max_tokens if existing else None),
        )
    return list(table.values())


class OpenAICompatible(Service):
    """Registers the OpenAI-compatible adapter for its provider table."""

    provide = "openai_compatible"
    inject = ["llm"]

    #: The adapter class the plugin registers. A subclass overrides this rather
    #: than reimplementing the registration.
    adapter_class = OpenAICompatibleAdapter

    #: The providers this plugin owns when config adds none.
    default_providers = DEFAULT_PROVIDERS

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        self._root = getattr(ctx, "root", ctx)
        self.providers = merge_providers(
            self.default_providers, config.get("providers")
        )
        self._by_name = {p.provider: p for p in self.providers}
        self.adapter = self.adapter_class(
            self.endpoint_for, self.api_key_for, config.get("transport")
        )
        release = ctx.llm.register_adapter(
            [p.provider for p in self.providers], self.adapter
        )
        ctx.effect(lambda: release)

    def endpoint_for(self, provider: str) -> ProviderConfig:
        endpoint = self._by_name.get(provider)
        if endpoint is None:
            known = ", ".join(sorted(self._by_name)) or "none"
            raise LlmError(
                f"no provider {provider!r} is configured (known: {known})",
                "NO_ADAPTER",
            )
        return endpoint

    async def api_key_for(self, provider: str) -> str:
        """Resolve a provider's key **now**, through `ctx.credentials`.

        Per call, not at startup: a key rotated an hour ago should work without
        a restart, and that is the whole reason the credentials service
        resolves at the moment of use.
        """
        endpoint = self.endpoint_for(provider)
        if not endpoint.api_key_ref:
            return ""
        credentials = getattr(self._root, "credentials", None)
        if credentials is None:
            if endpoint.allow_empty_key:
                return ""
            raise LlmError(
                f"provider {provider!r} needs the credential "
                f"{endpoint.api_key_ref!r}, but ctx.credentials is not mounted",
                "MISSING_CREDENTIAL",
            )
        resolved = await credentials.resolve(endpoint.api_key_ref)
        return (resolved or {}).get("value", "")


__all__ = [
    "OpenAICompatible",
    "OpenAICompatibleAdapter",
    "ProviderConfig",
    "DEFAULT_PROVIDERS",
    "merge_providers",
    "serialize_messages",
    "serialize_request",
    "translate",
    "map_finish_reason",
    "map_usage",
    "FINISH_REASONS",
    "DONE",
    "DATA_PREFIX",
    "NO_OUTPUT",
    "COMPLETIONS_PATH",
    "PLACEHOLDER_KEY",
]
