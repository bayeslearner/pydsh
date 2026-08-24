"""The LLM seam on a real kernel context — Requirement 4 and invariant I5.

Uses a real plugkit ``Context`` so mounting, the routing table, the
``llm/stream`` waterfall, and the retry latch are exercised together rather
than against mocks of the kernel.
"""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from plugkit import Context

from pydsh.llm import (
    ADAPTERS_UPDATED,
    ChunkType,
    GenerateOptions,
    LlmAdapter,
    LlmError,
    LlmProviderInfo,
    LlmService,
    ResolvedRetryPolicy,
    StreamChunk,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class ScriptedAdapter(LlmAdapter):
    """Emits a scripted list of chunks, optionally failing after ``fail_after``.

    ``calls`` records the options of every attempt, which is how the retry
    tests tell one attempt from a replay.
    """

    def __init__(self, texts=("a", "b"), fail_after=None, code="SERVER", fail_times=0):
        self.texts = list(texts)
        self.fail_after = fail_after
        self.code = code
        self.fail_times = fail_times
        self.calls: list[GenerateOptions] = []

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        self.calls.append(options)
        attempt = len(self.calls)
        for i, text in enumerate(self.texts):
            if self.fail_after is not None and i == self.fail_after:
                if attempt <= self.fail_times or self.fail_times == 0:
                    raise LlmError("scripted failure", code=self.code)
            yield StreamChunk(type=ChunkType.TEXT_DELTA, index=i, text=text)
        yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=f"{provider} (scripted)")


def options(provider="acme", model="a-1", **kw) -> GenerateOptions:
    return GenerateOptions(provider=provider, model=model, messages=[], **kw)


async def collect(stream) -> list[StreamChunk]:
    return [chunk async for chunk in stream]


async def mounted() -> Context:
    root = Context()
    await root.plugin(LlmService)
    return root


# --------------------------------------------------------------------------- #
# Mounting and the routing table
# --------------------------------------------------------------------------- #
async def test_service_mounts_and_resolves():
    root = await mounted()
    assert root.llm is not None
    assert "llm" in root


async def test_register_binds_every_route():
    root = await mounted()
    handle = root.llm.register_adapter(["acme", "beta"], ScriptedAdapter())
    assert handle.providers == {"acme", "beta"}
    assert {p.id for p in root.llm.list_providers()} == {"acme", "beta"}


async def test_duplicate_registration_is_rejected_and_binds_nothing():
    """All-or-nothing: the free route must not be bound by a failed call."""
    root = await mounted()
    root.llm.register_adapter(["acme"], ScriptedAdapter())
    with pytest.raises(RuntimeError, match="already has an adapter"):
        root.llm.register_adapter(["fresh", "acme"], ScriptedAdapter())
    assert {p.id for p in root.llm.list_providers()} == {"acme"}


async def test_empty_provider_name_rejected():
    root = await mounted()
    with pytest.raises(ValueError, match="must not be empty"):
        root.llm.register_adapter([""], ScriptedAdapter())


async def test_replace_takes_over_a_route():
    root = await mounted()
    first, second = ScriptedAdapter(), ScriptedAdapter()
    root.llm.register_adapter(["acme"], first)
    root.llm.register_adapter(["acme"], second, replace=True)
    chunks = await collect(root.llm.stream(options()))
    assert second.calls and not first.calls
    assert chunks


async def test_release_removes_only_its_own_routes():
    root = await mounted()
    a = root.llm.register_adapter(["acme"], ScriptedAdapter())
    root.llm.register_adapter(["beta"], ScriptedAdapter())
    a()
    assert {p.id for p in root.llm.list_providers()} == {"beta"}


async def test_release_is_idempotent():
    root = await mounted()
    handle = root.llm.register_adapter(["acme"], ScriptedAdapter())
    handle()
    handle()
    assert root.llm.list_providers() == []


async def test_release_does_not_evict_a_replacement():
    """After replace=True took the route, the old handle must not drop it."""
    root = await mounted()
    first = root.llm.register_adapter(["acme"], ScriptedAdapter())
    root.llm.register_adapter(["acme"], ScriptedAdapter(), replace=True)
    first()
    assert {p.id for p in root.llm.list_providers()} == {"acme"}


async def test_handle_replace_moves_the_route_set():
    root = await mounted()
    handle = root.llm.register_adapter(["acme"], ScriptedAdapter())
    handle.replace(["gamma"])
    assert {p.id for p in root.llm.list_providers()} == {"gamma"}


async def test_registration_broadcasts_topology_change():
    root = await mounted()
    seen = []
    root.on(ADAPTERS_UPDATED, lambda: seen.append(1))
    handle = root.llm.register_adapter(["acme"], ScriptedAdapter())
    handle()
    assert len(seen) == 2  # one bind, one release


async def test_failing_observer_cannot_break_the_commit():
    """Requirement 4.7 — the registration already happened."""
    root = await mounted()

    def boom():
        raise RuntimeError("observer exploded")

    root.on(ADAPTERS_UPDATED, boom)
    root.llm.register_adapter(["acme"], ScriptedAdapter())
    assert {p.id for p in root.llm.list_providers()} == {"acme"}


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #
async def test_stream_yields_adapter_chunks():
    root = await mounted()
    root.llm.register_adapter(["acme"], ScriptedAdapter(texts=("x", "y")))
    chunks = await collect(root.llm.stream(options()))
    assert [c.text for c in chunks if c.type is ChunkType.TEXT_DELTA] == ["x", "y"]
    assert chunks[-1].type is ChunkType.FINISH


async def test_stream_without_adapter_names_the_provider():
    root = await mounted()
    with pytest.raises(LlmError, match="nowhere"):
        await collect(root.llm.stream(options(provider="nowhere")))


async def test_provider_defaults_fill_the_lowest_layer():
    """Requirement 4.4 — the route's default model reaches the adapter."""
    root = await mounted()
    adapter = ScriptedAdapter()
    root.llm.register_adapter(["acme"], adapter, defaults={"model": "default-model"})
    await collect(root.llm.stream(GenerateOptions("acme", "", [])))
    assert adapter.calls[0].model == "default-model"


async def test_request_beats_provider_defaults():
    root = await mounted()
    adapter = ScriptedAdapter()
    root.llm.register_adapter(["acme"], adapter, defaults={"model": "default-model"})
    await collect(root.llm.stream(options(model="explicit")))
    assert adapter.calls[0].model == "explicit"


async def test_middleware_can_wrap_the_stream():
    """Requirement 4.5 — a listener sees the options and wraps `next`."""
    root = await mounted()
    root.llm.register_adapter(["acme"], ScriptedAdapter(texts=("x",)))
    seen = []

    def middleware(opts, next_):
        seen.append(opts.model)

        async def wrapped():
            async for chunk in next_():
                if chunk.type is ChunkType.TEXT_DELTA:
                    chunk.text = chunk.text.upper()
                yield chunk

        return wrapped()

    root.on("llm/stream", middleware)
    chunks = await collect(root.llm.stream(options(model="a-1")))
    assert seen == ["a-1"]
    assert [c.text for c in chunks if c.type is ChunkType.TEXT_DELTA] == ["X"]


async def test_middleware_may_replace_the_stream_entirely():
    root = await mounted()
    root.llm.register_adapter(["acme"], ScriptedAdapter())

    def middleware(opts, next_):
        async def canned():
            yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text="cached")

        return canned()

    root.on("llm/stream", middleware)
    chunks = await collect(root.llm.stream(options()))
    assert [c.text for c in chunks] == ["cached"]


# --------------------------------------------------------------------------- #
# Retry (Requirement 3.5 / invariant I5 / Property 3)
# --------------------------------------------------------------------------- #
async def test_retry_before_any_chunk_succeeds_on_the_second_attempt():
    root = await mounted()
    adapter = ScriptedAdapter(texts=("x",), fail_after=0, code="SERVER", fail_times=1)
    policy = ResolvedRetryPolicy(mode="normal", max_retries=2, initial_delay_ms=1)
    root.llm.register_adapter(["acme"], adapter, retry=policy)
    chunks = await collect(root.llm.stream(options()))
    assert len(adapter.calls) == 2
    assert [c.text for c in chunks if c.type is ChunkType.TEXT_DELTA] == ["x"]


async def test_retry_never_duplicates_emitted_chunks():
    """Property 3 — k chunks then failure yields exactly k, then the error."""
    root = await mounted()
    adapter = ScriptedAdapter(texts=("a", "b", "c"), fail_after=2, code="SERVER")
    policy = ResolvedRetryPolicy(mode="always", initial_delay_ms=1)
    root.llm.register_adapter(["acme"], adapter, retry=policy)

    seen: list[str] = []
    with pytest.raises(LlmError):
        async for chunk in root.llm.stream(options()):
            if chunk.type is ChunkType.TEXT_DELTA:
                seen.append(chunk.text)

    assert seen == ["a", "b"]  # not ["a","b","a","b",...]
    assert len(adapter.calls) == 1


async def test_non_retryable_code_propagates():
    root = await mounted()
    adapter = ScriptedAdapter(texts=("x",), fail_after=0, code="AUTH")
    policy = ResolvedRetryPolicy(mode="normal", max_retries=5, initial_delay_ms=1)
    root.llm.register_adapter(["acme"], adapter, retry=policy)
    with pytest.raises(LlmError, match="AUTH"):
        await collect(root.llm.stream(options()))
    assert len(adapter.calls) == 1


async def test_without_a_policy_the_first_failure_surfaces():
    root = await mounted()
    adapter = ScriptedAdapter(texts=("x",), fail_after=0)
    root.llm.register_adapter(["acme"], adapter)
    with pytest.raises(LlmError):
        await collect(root.llm.stream(options()))
    assert len(adapter.calls) == 1


async def test_max_retries_is_a_ceiling():
    root = await mounted()
    adapter = ScriptedAdapter(texts=("x",), fail_after=0, code="SERVER")
    policy = ResolvedRetryPolicy(mode="normal", max_retries=2, initial_delay_ms=1)
    root.llm.register_adapter(["acme"], adapter, retry=policy)
    with pytest.raises(LlmError):
        await collect(root.llm.stream(options()))
    assert len(adapter.calls) == 3  # original + 2 retries


# --------------------------------------------------------------------------- #
# Model resolution
# --------------------------------------------------------------------------- #
async def test_resolve_model_info_rejects_a_mismatched_id():
    class Liar(ScriptedAdapter):
        async def resolve_model(self, provider, model):
            return {"provider": provider, "id": "something-else", "name": model}

    root = await mounted()
    root.llm.register_adapter(["acme"], Liar())
    with pytest.raises(LlmError, match="expected"):
        await root.llm.resolve_model_info("acme", "a-1")


async def test_resolve_model_info_returns_adapter_metadata():
    root = await mounted()
    root.llm.register_adapter(["acme"], ScriptedAdapter())
    info = await root.llm.resolve_model_info("acme", "a-1")
    assert info["id"] == "a-1"
