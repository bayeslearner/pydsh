"""The DeepSeek adapter — Requirement 5.

The classification tests are the point. A 400 that means "your prompt is too
long" and a 400 that means "you sent a field I don't know" want opposite
responses from the caller, and only the body tells them apart.
"""

from __future__ import annotations

import asyncio
import email.utils
import json
import time
from typing import Any, AsyncIterator

import pytest

from plugkit import Context

from pydsh import (
    ChunkType,
    Credentials,
    DeepSeek,
    GenerateOptions,
    LlmError,
    LlmService,
)
from pydsh.llm.adapters.deepseek import (
    CONTEXT_WINDOW_EXCEEDED,
    DEFAULT_CONTEXT_WINDOW,
    QUOTA_EXCEEDED,
    DeepSeekAdapter,
    error_detail,
    http_error_code,
    is_context_window_exceeded,
    is_quota_exceeded,
    request_id,
    resolve_thinking,
    serialize_deepseek_request,
)
from pydsh.llm.adapters.openai_compatible import OpenAICompatibleAdapter
from pydsh.llm.adapters.transport import with_idle_timeout

pytestmark = pytest.mark.asyncio


def sse(*frames: Any, done: bool = True) -> list[str]:
    lines = [f"data: {json.dumps(f)}" for f in frames]
    if done:
        lines.append("data: [DONE]")
    return lines


def frame(**delta: Any) -> dict:
    return {"choices": [{"index": 0, "delta": delta}]}


def finish_frame(reason: str) -> dict:
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def scripted(lines: list[str], record: Any = None):
    async def transport(url, body, headers, signal=None):
        if record is not None:
            record.append({"url": url, "body": body, "headers": headers})
        for line in lines:
            yield line

    return transport


def failing(error: LlmError):
    async def transport(url, body, headers, signal=None):
        raise error
        yield  # pragma: no cover - keeps this a generator

    return transport


async def build(transport, key: str = "sk-test"):
    root = Context()
    await root.plugin(LlmService)
    await root.plugin(Credentials)
    if key is not None:
        await root.credentials.set("DEEPSEEK_API_KEY", key)
    await root.plugin(DeepSeek, {"transport": transport})
    return root


# --------------------------------------------------------------------------- #
# R5.1 — it extends rather than copies
# --------------------------------------------------------------------------- #
async def test_the_adapter_extends_the_openai_compatible_one():
    """R5.1 — the reference duplicates the serializer, and its bugs with it."""
    assert issubclass(DeepSeekAdapter, OpenAICompatibleAdapter)


async def test_it_registers_only_the_deepseek_route():
    root = await build(scripted([]))
    assert {info.id for info in root.llm.list_providers()} == {"deepseek"}


async def test_the_route_carries_deepseeks_context_window():
    root = await build(scripted([]))
    info = await root.llm.resolve_model_info("deepseek", "deepseek-chat")
    assert info["context"]["context_window"] == DEFAULT_CONTEXT_WINDOW


async def test_it_streams_the_shared_dialect():
    root = await build(scripted(sse(frame(content="hi"), finish_frame("stop"))))
    chunks = [
        c
        async for c in root.llm.stream(
            GenerateOptions(provider="deepseek", model="deepseek-chat", messages=[])
        )
    ]
    assert chunks[-1].finish == {"kind": "stop"}


# --------------------------------------------------------------------------- #
# R5.2 — classification
# --------------------------------------------------------------------------- #
async def test_auth_failures_are_auth():
    assert http_error_code(401) == "AUTH"
    assert http_error_code(403) == "AUTH"


async def test_a_spent_quota_is_told_apart_from_a_rate_limit():
    """A rate limit clears by waiting; a spent quota does not."""
    assert http_error_code(429, {"message": "insufficient balance"}) == QUOTA_EXCEEDED
    assert http_error_code(429, {"message": "too many requests"}) == "RATE_LIMIT"


async def test_a_context_overflow_is_told_apart_from_a_bad_request():
    """Compact and retry, versus fix the request — opposite responses."""
    overflow = http_error_code(400, {"message": "This model's maximum context length is 8192 tokens"})
    assert overflow == CONTEXT_WINDOW_EXCEEDED
    assert http_error_code(400, {"message": "unknown parameter: wibble"}) == "INVALID_REQUEST"


async def test_server_and_unknown_statuses():
    assert http_error_code(500) == "SERVER"
    assert http_error_code(503) == "SERVER"
    assert http_error_code(418) == "HTTP_418"


async def test_the_quota_and_context_patterns():
    assert is_quota_exceeded("insufficient quota")
    assert is_quota_exceeded("Your credits are exhausted")
    assert is_quota_exceeded("out of budget")
    assert not is_quota_exceeded("rate limit reached, slow down")

    assert is_context_window_exceeded("maximum context length exceeded")
    assert is_context_window_exceeded("the prompt is too long for this model")
    assert not is_context_window_exceeded("your answer was long")


async def test_error_detail_joins_the_fields_a_provider_uses():
    assert error_detail({"code": "x", "type": "y", "message": "z"}) == "x y z"
    assert error_detail(None) == ""
    assert error_detail({"message": "only this"}) == "only this"


async def test_a_transport_http_error_is_reclassified():
    """The transport can only say HTTP_400; the body says which kind."""
    root = await build(
        failing(LlmError("maximum context length is 8192", "HTTP_400"))
    )
    with pytest.raises(LlmError) as caught:
        async for _ in root.llm.stream(
            GenerateOptions(provider="deepseek", model="m", messages=[])
        ):
            pass
    assert caught.value.code == CONTEXT_WINDOW_EXCEEDED


async def test_a_non_http_error_passes_through_unchanged():
    root = await build(failing(LlmError("the socket died", "TRANSPORT")))
    with pytest.raises(LlmError) as caught:
        async for _ in root.llm.stream(
            GenerateOptions(provider="deepseek", model="m", messages=[])
        ):
            pass
    assert caught.value.code == "TRANSPORT"


async def test_retry_after_parses_seconds_and_dates():
    from pydsh.llm.adapters.deepseek import retry_after_seconds

    assert retry_after_seconds("30") == 30.0
    assert retry_after_seconds(None) is None
    assert retry_after_seconds("0") is None  # "retry now" is not a delay
    assert retry_after_seconds("not a date") is None

    now = time.time()
    future = email.utils.formatdate(now + 60, usegmt=True)
    delay = retry_after_seconds(future, now=now)
    assert delay is not None and 50 < delay <= 61

    past = email.utils.formatdate(now - 60, usegmt=True)
    assert retry_after_seconds(past, now=now) is None


async def test_request_id_reads_either_header():
    assert request_id({"x-request-id": "abc"}) == "abc"
    assert request_id({"x-deepseek-request-id": "def"}) == "def"
    assert request_id({}) is None
    assert request_id(None) is None


# --------------------------------------------------------------------------- #
# R5.3 — reasoning and thinking
# --------------------------------------------------------------------------- #
async def test_reasoning_content_comes_through_as_reasoning_blocks():
    """R5.3."""
    root = await build(
        scripted(sse(frame(reasoning_content="let me think"), frame(content="ok"), finish_frame("stop")))
    )
    chunks = [
        c
        async for c in root.llm.stream(
            GenerateOptions(provider="deepseek", model="deepseek-reasoner", messages=[])
        )
    ]
    reasoning = [c for c in chunks if c.type is ChunkType.REASONING_DELTA]
    assert [c.reasoning for c in reasoning] == ["let me think"]

    ends = [c for c in chunks if c.type is ChunkType.BLOCK_END]
    assert type(ends[0].block).__name__ == "ReasoningBlock"


async def test_reasoning_off_becomes_thinking_disabled_not_an_effort():
    """The two fields say contradictory things if both are sent."""
    body = serialize_deepseek_request(
        GenerateOptions(provider="deepseek", model="m", messages=[], reasoning_effort="off")
    )
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in body


async def test_a_real_effort_becomes_reasoning_effort_not_thinking():
    body = serialize_deepseek_request(
        GenerateOptions(provider="deepseek", model="m", messages=[], reasoning_effort="high")
    )
    assert body["reasoning_effort"] == "high"
    assert "thinking" not in body


async def test_no_effort_sends_neither_field():
    body = serialize_deepseek_request(
        GenerateOptions(provider="deepseek", model="m", messages=[])
    )
    assert "thinking" not in body and "reasoning_effort" not in body


async def test_an_unsupported_effort_is_refused():
    with pytest.raises(LlmError) as caught:
        resolve_thinking("medium")
    assert caught.value.code == "UNSUPPORTED_REASONING_EFFORT"


async def test_the_body_the_transport_receives_carries_the_resolution():
    """End to end: the hook, not a helper nobody calls."""
    sent: list = []
    root = await build(scripted(sse(frame(content="hi"), finish_frame("stop")), sent))
    async for _ in root.llm.stream(
        GenerateOptions(
            provider="deepseek", model="m", messages=[], reasoning_effort="off"
        )
    ):
        pass
    assert sent[0]["body"]["thinking"] == {"type": "disabled"}


# --------------------------------------------------------------------------- #
# The idle timeout
# --------------------------------------------------------------------------- #
async def test_a_stalled_stream_times_out():
    """An endpoint that goes quiet looks exactly like a slow model otherwise."""

    async def stalls() -> AsyncIterator[str]:
        yield "data: {}"
        await asyncio.sleep(5)
        yield "data: [DONE]"  # pragma: no cover - never reached

    with pytest.raises(LlmError) as caught:
        async for _ in with_idle_timeout(stalls(), 0.05):
            pass
    assert caught.value.code == "TIMEOUT"


async def test_a_stream_that_keeps_talking_is_not_timed_out():
    """It bounds the *gap*, not the total: a long answer is still allowed."""

    async def slow_but_steady() -> AsyncIterator[str]:
        for _ in range(6):
            await asyncio.sleep(0.01)
            yield "data: {}"
        yield "data: [DONE]"

    lines = [line async for line in with_idle_timeout(slow_but_steady(), 0.2)]
    assert lines[-1] == "data: [DONE]"


async def test_the_idle_timeout_can_be_switched_off():
    adapter = DeepSeekAdapter(lambda p: None, lambda p: "", idle_timeout=0)
    stream = object()
    assert adapter.wrap_lines(stream, None) is stream
