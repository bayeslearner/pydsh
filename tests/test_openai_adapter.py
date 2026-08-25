"""The OpenAI-compatible adapter — Requirements 1–4, properties 1–3.

Every test drives real SSE bytes through a transport that never opens a socket.
That is not a shortcut around integration: it is the only way to test the cases
that matter here — a stream cut off mid-answer, a malformed frame, a provider
that numbers its tool calls from zero.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import pytest

from plugkit import Context

from pydsh import (
    ChunkType,
    Credentials,
    GenerateOptions,
    LlmError,
    LlmService,
    Message,
    MessageSource,
    OpenAICompatible,
    ProviderConfig,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    create_assistant_message,
    create_user_message,
)
from pydsh.llm.adapters.openai_compatible import (
    DEFAULT_PROVIDERS,
    NO_OUTPUT,
    OpenAICompatibleAdapter,
    map_finish_reason,
    map_usage,
    merge_providers,
    serialize_messages,
    serialize_request,
    translate,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Helpers — a transport that speaks SSE and never opens a socket
# --------------------------------------------------------------------------- #
def sse(*frames: Any, done: bool = True) -> list[str]:
    """Render frames as SSE lines, with the keep-alives a real server sends."""
    lines: list[str] = []
    for frame in frames:
        lines.append(f"data: {json.dumps(frame)}")
        lines.append("")  # servers separate events with a blank line
    if done:
        lines.append("data: [DONE]")
    return lines


def frame(**delta: Any) -> dict:
    return {"choices": [{"index": 0, "delta": delta}]}


def finish_frame(reason: str) -> dict:
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


async def lines_of(items: list[str]) -> AsyncIterator[str]:
    for item in items:
        yield item


def scripted(lines: list[str], record: Any = None):
    """A transport that replays `lines` and records what it was asked for."""

    async def transport(url, body, headers, signal=None):
        if record is not None:
            record.append({"url": url, "body": body, "headers": headers, "signal": signal})
        for line in lines:
            yield line

    return transport


async def payloads_of(lines: list[str]) -> AsyncIterator[str]:
    """The payload stream `translate` consumes, mirroring the adapter's filter."""
    for line in lines:
        line = line.strip()
        if line.startswith("data:"):
            yield line[len("data:"):].strip()


async def collect(lines: list[str]) -> list:
    return [chunk async for chunk in translate(payloads_of(lines))]


async def build(config: Any = None, key: str = "sk-test"):
    root = Context()
    await root.plugin(LlmService)
    await root.plugin(Credentials)
    if key is not None:
        await root.credentials.set("OPENAI_API_KEY", key)
    await root.plugin(OpenAICompatible, config or {})
    return root


# --------------------------------------------------------------------------- #
# R1 — serialization
# --------------------------------------------------------------------------- #
async def test_a_user_message_becomes_a_wire_message():
    wire = serialize_messages([create_user_message([TextBlock("hello")])])
    assert wire == [{"role": "user", "content": "hello"}]


async def test_an_empty_user_message_sends_an_empty_string_not_null():
    """R1.4 — absent and blank are different things to an endpoint."""
    wire = serialize_messages([create_user_message([])])
    assert wire == [{"role": "user", "content": ""}]


async def test_an_assistant_message_carries_its_tool_calls():
    message = create_assistant_message(
        [
            TextBlock("let me look"),
            ToolCallBlock(id="c1", name="bash", arguments='{"command":"ls"}'),
        ]
    )
    wire = serialize_messages([message])
    assert wire[0]["content"] == "let me look"
    assert wire[0]["tool_calls"] == [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"command":"ls"}'},
        }
    ]


async def test_reasoning_is_sent_back_only_with_tool_calls():
    """R1.3 — reasoning replayed without the call it led to is unusable."""
    with_call = create_assistant_message(
        [ReasoningBlock(text="thinking"), ToolCallBlock(id="c1", name="b", arguments="{}")]
    )
    alone = create_assistant_message([ReasoningBlock(text="thinking"), TextBlock("hi")])

    assert serialize_messages([with_call])[0]["reasoning_content"] == "thinking"
    assert "reasoning_content" not in serialize_messages([alone])[0]


async def test_nothing_separates_a_call_from_its_result():
    """Property 3 (R1.2, I2) — the reference emits the user text first.

    An endpoint requires the `role: "tool"` messages answering a call to follow
    that call with nothing in between; a user message in the gap is rejected,
    or worse, silently mis-attributed.
    """
    assistant = create_assistant_message(
        [ToolCallBlock(id="c1", name="bash", arguments="{}")]
    )
    answer = create_user_message(
        [
            ToolResultBlock(tool_call_id="c1", content=(TextBlock("ok"),), is_error=False),
            TextBlock("actually, stop"),
        ]
    )

    wire = serialize_messages([assistant, answer])
    roles = [m["role"] for m in wire]
    assert roles == ["assistant", "tool", "user"]
    assert wire[1]["tool_call_id"] == "c1"


async def test_two_tool_results_both_precede_the_text():
    answer = create_user_message(
        [
            ToolResultBlock(tool_call_id="c1", content=(TextBlock("a"),), is_error=False),
            ToolResultBlock(tool_call_id="c2", content=(TextBlock("b"),), is_error=False),
            TextBlock("and now this"),
        ]
    )
    roles = [m["role"] for m in serialize_messages([answer])]
    assert roles == ["tool", "tool", "user"]


async def test_a_tool_result_with_no_output_sends_a_placeholder():
    answer = create_user_message([ToolResultBlock(tool_call_id="c1", content=(), is_error=False)])
    wire = serialize_messages([answer])
    assert wire == [{"role": "tool", "tool_call_id": "c1", "content": NO_OUTPUT}]


async def test_a_message_with_only_tool_results_sends_no_user_message():
    answer = create_user_message(
        [ToolResultBlock(tool_call_id="c1", content=(TextBlock("done"),), is_error=False)]
    )
    assert [m["role"] for m in serialize_messages([answer])] == ["tool"]


async def test_the_request_always_streams_and_asks_for_usage():
    """R1.5."""
    body = serialize_request(GenerateOptions(provider="p", model="m", messages=[]))
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["model"] == "m"


async def test_optional_fields_are_omitted_when_unset():
    """R1.6 — an absent field lets the provider's default apply."""
    body = serialize_request(GenerateOptions(provider="p", model="m", messages=[]))
    for field in ("tools", "temperature", "max_tokens", "stop", "reasoning_effort"):
        assert field not in body


async def test_optional_fields_are_carried_when_set():
    body = serialize_request(
        GenerateOptions(
            provider="p",
            model="m",
            messages=[],
            system="be brief",
            tools=[{"name": "bash", "description": "run", "parameters": {"x": 1}}],
            temperature=0.2,
            max_tokens=100,
            stop=["END"],
            reasoning_effort="high",
        )
    )
    assert body["messages"][0] == {"role": "system", "content": "be brief"}
    assert body["tools"][0]["function"]["name"] == "bash"
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 100
    assert body["stop"] == ["END"]
    assert body["reasoning_effort"] == "high"


# --------------------------------------------------------------------------- #
# R2 — translation
# --------------------------------------------------------------------------- #
async def test_text_streams_as_deltas_then_one_block_end():
    chunks = await collect(sse(frame(content="he"), frame(content="llo"), finish_frame("stop")))
    kinds = [c.type for c in chunks]
    assert kinds == [
        ChunkType.BLOCK_START,
        ChunkType.TEXT_DELTA,
        ChunkType.TEXT_DELTA,
        ChunkType.BLOCK_END,
        ChunkType.FINISH,
    ]
    assert chunks[-2].block.text == "hello"
    assert chunks[-1].finish == {"kind": "stop"}


async def test_text_and_a_tool_call_get_distinct_indices():
    """Property 1 (R2.2, I1) — the reference gives both index 0.

    The wire numbers its *tool calls* from zero however much text came first,
    so using that number as the block index makes two blocks with one identity.
    """
    chunks = await collect(
        sse(
            frame(content="let me check"),
            frame(
                tool_calls=[
                    {"index": 0, "id": "c1", "function": {"name": "bash", "arguments": "{}"}}
                ]
            ),
            finish_frame("tool_calls"),
        )
    )
    starts = [c for c in chunks if c.type is ChunkType.BLOCK_START]
    assert [s.block_type for s in starts] == ["text", "tool-call"]
    assert len({s.index for s in starts}) == 2, "two blocks claimed one index"


async def test_every_block_in_a_full_response_has_its_own_index():
    """Property 1 — reasoning, text and two tool calls at once."""
    chunks = await collect(
        sse(
            frame(reasoning_content="hmm"),
            frame(content="ok"),
            frame(tool_calls=[{"index": 0, "id": "c1", "function": {"name": "a", "arguments": "{}"}}]),
            frame(tool_calls=[{"index": 1, "id": "c2", "function": {"name": "b", "arguments": "{}"}}]),
            finish_frame("tool_calls"),
        )
    )
    starts = [c for c in chunks if c.type is ChunkType.BLOCK_START]
    assert [s.block_type for s in starts] == ["reasoning", "text", "tool-call", "tool-call"]
    assert len({s.index for s in starts}) == 4


async def test_block_ends_come_in_the_order_the_blocks_opened():
    """R2.3."""
    chunks = await collect(
        sse(
            frame(reasoning_content="hmm"),
            frame(content="ok"),
            frame(tool_calls=[{"index": 0, "id": "c1", "function": {"name": "a", "arguments": "{}"}}]),
            finish_frame("tool_calls"),
        )
    )
    ends = [c for c in chunks if c.type is ChunkType.BLOCK_END]
    assert [type(e.block).__name__ for e in ends] == [
        "ReasoningBlock",
        "TextBlock",
        "ToolCallBlock",
    ]


async def test_a_tool_call_accumulates_its_arguments_and_a_late_name():
    chunks = await collect(
        sse(
            frame(tool_calls=[{"index": 0, "id": "c1", "function": {"arguments": '{"a'}}]),
            frame(tool_calls=[{"index": 0, "function": {"name": "bash", "arguments": '":1}'}}]),
            finish_frame("tool_calls"),
        )
    )
    end = next(c for c in chunks if c.type is ChunkType.BLOCK_END)
    assert end.block.id == "c1"
    assert end.block.name == "bash"
    assert end.block.arguments == '{"a":1}'


async def test_finish_reasons_map():
    """R2.4."""
    assert map_finish_reason("stop") == {"kind": "stop"}
    assert map_finish_reason("tool_calls") == {"kind": "tool-calls"}
    assert map_finish_reason("length") == {"kind": "max-tokens"}

    filtered = map_finish_reason("content_filter")
    assert filtered["kind"] == "error"
    assert filtered["failure"]["code"] == "CONTENT_FILTER"


async def test_usage_keeps_its_counts_disjoint():
    """R2.5 — cache reads arrive *inside* prompt_tokens."""
    mapped = map_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 800},
            "completion_tokens_details": {"reasoning_tokens": 20},
        }
    )
    assert mapped == {
        "input_tokens": 200,
        "output_tokens": 50,
        "cache_read_tokens": 800,
        "reasoning_tokens": 20,
    }
    assert mapped["input_tokens"] + mapped["cache_read_tokens"] == 1000


async def test_usage_falls_back_to_the_deepseek_cache_field():
    mapped = map_usage(
        {"prompt_tokens": 100, "completion_tokens": 5, "prompt_cache_hit_tokens": 60}
    )
    assert mapped["input_tokens"] == 40 and mapped["cache_read_tokens"] == 60


async def test_usage_reaches_the_stream_before_the_finish():
    chunks = await collect(
        sse(
            frame(content="hi"),
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
            finish_frame("stop"),
        )
    )
    kinds = [c.type for c in chunks]
    assert kinds[-2:] == [ChunkType.USAGE, ChunkType.FINISH]


async def test_a_completed_response_with_no_content_is_an_error():
    """R2.6 — an empty turn recorded as a success is worse than a failure."""
    chunks = await collect(sse(finish_frame("stop")))
    assert chunks[-1].finish["failure"]["code"] == "EMPTY_RESPONSE"


async def test_a_malformed_payload_raises():
    """R2.7."""
    with pytest.raises(LlmError) as caught:
        await collect(["data: {not json"])
    assert caught.value.code == "MALFORMED_RESPONSE"


async def test_a_stream_that_ends_before_done_raises():
    """Property 2 (R2.8, I4) — half an answer must not look like a whole one."""
    with pytest.raises(LlmError) as caught:
        await collect(sse(frame(content="the answer is"), done=False))
    assert caught.value.code == "STREAM_CLOSED"


async def test_every_truncation_point_raises():
    """Property 2 — at *any* prefix, not just the convenient one."""
    full = sse(
        frame(reasoning_content="hmm"),
        frame(content="ok"),
        frame(tool_calls=[{"index": 0, "id": "c1", "function": {"name": "a", "arguments": "{}"}}]),
        finish_frame("tool_calls"),
    )
    for cut in range(len(full)):  # every prefix short of the [DONE] line
        with pytest.raises(LlmError) as caught:
            await collect(full[:cut])
        assert caught.value.code == "STREAM_CLOSED"


async def test_keep_alives_and_comments_are_skipped():
    chunks = await collect(
        [": ping", "", "event: message", *sse(frame(content="hi"), finish_frame("stop"))]
    )
    assert chunks[-1].finish == {"kind": "stop"}


# --------------------------------------------------------------------------- #
# R3 — the adapter
# --------------------------------------------------------------------------- #
async def test_the_adapter_streams_end_to_end():
    calls: list = []
    root = await build({"transport": scripted(sse(frame(content="hi"), finish_frame("stop")), calls)})

    chunks = [
        c
        async for c in root.llm.stream(
            GenerateOptions(provider="openai", model="gpt-x", messages=[])
        )
    ]
    assert chunks[-1].finish == {"kind": "stop"}
    assert calls[0]["url"] == "https://api.openai.com/v1/chat/completions"
    assert calls[0]["headers"]["authorization"] == "Bearer sk-test"
    assert calls[0]["headers"]["accept"] == "text/event-stream"


async def test_the_transport_receives_the_cancel_signal():
    """R3.4, I5."""
    calls: list = []
    root = await build({"transport": scripted(sse(frame(content="hi"), finish_frame("stop")), calls)})

    class Signal:
        aborted = False

    signal = Signal()
    async for _ in root.llm.stream(
        GenerateOptions(provider="openai", model="m", messages=[], signal=signal)
    ):
        pass
    assert calls[0]["signal"] is signal


async def test_an_aborted_signal_cuts_the_payload_stream():
    class Signal:
        aborted = True

    root = await build({"transport": scripted(sse(frame(content="hi"), finish_frame("stop")))})
    with pytest.raises(LlmError) as caught:
        async for _ in root.llm.stream(
            GenerateOptions(provider="openai", model="m", messages=[], signal=Signal())
        ):
            pass
    # No [DONE] was ever consumed, so the stream is reported as what it is.
    assert caught.value.code == "STREAM_CLOSED"


async def test_a_missing_key_is_refused_before_any_request():
    """R3.2 — a remote 401 would say the key is wrong, not that it is absent."""
    sent: list = []
    root = await build({"transport": scripted([], sent)}, key=None)

    with pytest.raises(LlmError) as caught:
        async for _ in root.llm.stream(
            GenerateOptions(provider="openai", model="m", messages=[])
        ):
            pass
    assert caught.value.code == "MISSING_CREDENTIAL"
    assert "OPENAI_API_KEY" in str(caught.value)
    assert sent == []


async def test_an_illegal_key_is_refused_and_never_echoed():
    root = await build({"transport": scripted([])}, key="sk with a space\n")
    with pytest.raises(LlmError) as caught:
        async for _ in root.llm.stream(
            GenerateOptions(provider="openai", model="m", messages=[])
        ):
            pass
    assert caught.value.code == "ILLEGAL_API_KEY"
    assert "sk with" not in str(caught.value)


async def test_a_provider_that_allows_an_empty_key_works_without_one():
    """R3.3 — Ollama and vLLM need no credential."""
    calls: list = []
    root = await build(
        {"transport": scripted(sse(frame(content="hi"), finish_frame("stop")), calls)},
        key=None,
    )
    chunks = [
        c
        async for c in root.llm.stream(
            GenerateOptions(provider="ollama", model="llama", messages=[])
        )
    ]
    assert chunks[-1].finish == {"kind": "stop"}
    assert calls[0]["headers"]["authorization"] == "Bearer not-needed"


async def test_provider_info_reports_the_display_name():
    """R3.6."""
    root = await build({"transport": scripted([])})
    assert root.openai_compatible.adapter.provider_info("openai").name == "OpenAI"


async def test_resolve_model_carries_the_context_window():
    root = await build(
        {
            "transport": scripted([]),
            "providers": [{"provider": "openai", "context_window": 128_000}],
        }
    )
    info = await root.llm.resolve_model_info("openai", "gpt-x")
    assert info["context"]["context_window"] == 128_000


# --------------------------------------------------------------------------- #
# R4 — the provider table
# --------------------------------------------------------------------------- #
async def test_the_seven_defaults_are_registered_dormant():
    """R4.1 — routable, and unusable until a credential resolves."""
    root = await build({"transport": scripted([])}, key=None)
    registered = {info.id for info in root.llm.list_providers()}
    assert registered == {
        "openai", "qwen", "zhipu", "moonshot", "deepseek", "ollama", "vllm"
    }


async def test_no_default_reads_the_environment_at_import_time(monkeypatch):
    """R4.2, I3 — the reference builds two base URLs from os.environ on import."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://somewhere-else:9999/v1")
    import importlib

    from pydsh.llm.adapters import openai_compatible

    reloaded = importlib.reload(openai_compatible)
    ollama = next(p for p in reloaded.DEFAULT_PROVIDERS if p.provider == "ollama")
    assert "somewhere-else" not in ollama.base_url


async def test_config_overrides_a_default_by_name():
    """R4.3."""
    merged = merge_providers(
        DEFAULT_PROVIDERS, [{"provider": "openai", "base_url": "http://proxy/v1"}]
    )
    openai = next(p for p in merged if p.provider == "openai")
    assert openai.base_url == "http://proxy/v1"
    # Everything it did not name is kept, rather than reset to a default.
    assert openai.api_key_ref == "OPENAI_API_KEY"
    assert openai.display_name == "OpenAI"


async def test_config_adds_a_new_provider():
    merged = merge_providers(
        DEFAULT_PROVIDERS,
        [{"provider": "acme", "base_url": "http://acme/v1", "api_key_ref": "ACME_KEY"}],
    )
    assert any(p.provider == "acme" for p in merged)
    assert len(merged) == len(DEFAULT_PROVIDERS) + 1


async def test_a_new_provider_without_a_base_url_is_refused():
    with pytest.raises(LlmError) as caught:
        merge_providers(DEFAULT_PROVIDERS, [{"provider": "acme"}])
    assert caught.value.code == "INVALID_REQUEST"


async def test_an_override_without_a_name_is_refused():
    with pytest.raises(LlmError):
        merge_providers(DEFAULT_PROVIDERS, [{"base_url": "http://x"}])


async def test_an_unknown_provider_raises_no_adapter():
    """R4.5."""
    root = await build({"transport": scripted([])})
    with pytest.raises(LlmError) as caught:
        root.openai_compatible.endpoint_for("nowhere")
    assert caught.value.code == "NO_ADAPTER"


async def test_a_key_resolves_per_call_not_at_startup():
    """R4.4 — a rotated key takes effect without a restart."""
    calls: list = []
    root = await build({"transport": scripted(sse(frame(content="a"), finish_frame("stop")), calls)})

    await root.credentials.set("OPENAI_API_KEY", "sk-rotated")
    async for _ in root.llm.stream(
        GenerateOptions(provider="openai", model="m", messages=[])
    ):
        pass
    assert calls[-1]["headers"]["authorization"] == "Bearer sk-rotated"


async def test_without_credentials_mounted_the_ref_is_named():
    root = Context()
    await root.plugin(LlmService)
    await root.plugin(OpenAICompatible, {"transport": scripted([])})

    with pytest.raises(LlmError) as caught:
        await root.openai_compatible.api_key_for("openai")
    assert caught.value.code == "MISSING_CREDENTIAL"
    assert "OPENAI_API_KEY" in str(caught.value)
