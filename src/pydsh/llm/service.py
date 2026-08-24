"""``ctx.llm`` — the adapter registry and the interceptable stream.

The seam does two things. It **owns the routing table** from provider name to
adapter, handing back a handle that can release exactly what it bound. And it
**streams**, resolving the effective call config first, then dispatching
through the ``llm/stream`` waterfall so middleware can wrap the adapter without
the caller knowing.

Nothing here speaks a wire protocol (invariant I1): the adapter ABC is the only
door out.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Iterable, Optional

from plugkit import Service

from ..dispatch import emit_contained
from .adapter import LlmAdapter, LlmProviderInfo
from .call_config import call_config_from_options, merge_call_config
from .chunks import GenerateOptions, StreamChunk
from .errors import LlmError
from .retry import ResolvedRetryPolicy

#: Broadcast whenever the routing table changes (bind, replace, or release).
ADAPTERS_UPDATED = "llm/adapters-updated"

#: The waterfall middleware wraps.
STREAM_WATERFALL = "llm/stream"


class AdapterRegistration:
    """A handle over the routes one ``register_adapter`` call bound.

    Calling the handle releases those routes. :meth:`replace` swaps the route
    set atomically while keeping the same adapter instance. Both are
    idempotent — releasing twice is a no-op, not an error.
    """

    def __init__(self, service: "LlmService", adapter: LlmAdapter, providers: set[str]):
        self._service = service
        self._adapter = adapter
        self._owned = set(providers)

    @property
    def providers(self) -> set[str]:
        """The routes this registration currently holds."""
        return set(self._owned)

    def __call__(self) -> None:
        """Release every route this registration still holds."""
        if not self._owned:
            return
        self._service._release(self._adapter, self._owned)
        self._owned.clear()
        self._service._announce()

    def replace(self, next_providers: Iterable[str]) -> None:
        """Atomically move this registration onto a different route set."""
        candidates = list(next_providers)
        self._service._assert_bindable(candidates, exempt=self._owned)
        self._service._release(self._adapter, self._owned)
        self._owned.clear()
        self._service._bind(self._adapter, candidates, self._owned)
        self._service._announce()


class LlmService(Service):
    """The ``llm`` service."""

    provide = "llm"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._adapters: dict[str, LlmAdapter] = {}
        self._provider_retry: dict[str, ResolvedRetryPolicy] = {}
        self._provider_defaults: dict[str, dict] = {}

    # -- routing table ---------------------------------------------------- #

    def _assert_bindable(
        self, providers: Iterable[str], exempt: Optional[set[str]] = None
    ) -> None:
        """Check every route is free before binding any (all-or-nothing)."""
        exempt = exempt or set()
        for provider in providers:
            if not provider:
                raise ValueError("a provider name must not be empty")
            if provider in self._adapters and provider not in exempt:
                raise RuntimeError(f"provider {provider!r} already has an adapter")

    def _bind(
        self, adapter: LlmAdapter, providers: Iterable[str], owned: set[str]
    ) -> None:
        for provider in providers:
            self._adapters[provider] = adapter
            owned.add(provider)

    def _release(self, adapter: LlmAdapter, providers: Iterable[str]) -> None:
        for provider in list(providers):
            # Only drop a route this adapter still owns — a `replace=True`
            # registration may have taken it over in the meantime.
            if self._adapters.get(provider) is adapter:
                self._adapters.pop(provider, None)
                self._provider_retry.pop(provider, None)
                self._provider_defaults.pop(provider, None)

    def _announce(self) -> None:
        """Post-commit topology notice; a failing observer cannot undo it."""
        emit_contained(self.ctx, ADAPTERS_UPDATED)

    def register_adapter(
        self,
        providers: list[str],
        adapter: LlmAdapter,
        *,
        replace: bool = False,
        retry: Optional[ResolvedRetryPolicy] = None,
        defaults: Optional[dict] = None,
    ) -> AdapterRegistration:
        """Route a set of providers to one adapter — all of them, or none.

        :param replace: allow taking over routes another registration holds.
        :param retry: the route's retry policy; without one, failures surface
            immediately.
        :param defaults: the route's default call config — the lowest merge
            layer, e.g. a default model.
        """
        if not replace:
            self._assert_bindable(providers)
        else:
            for provider in providers:
                if not provider:
                    raise ValueError("a provider name must not be empty")

        owned: set[str] = set()
        self._bind(adapter, providers, owned)
        for provider in providers:
            if retry is not None:
                self._provider_retry[provider] = retry
            if defaults is not None:
                self._provider_defaults[provider] = defaults
        self._announce()
        return AdapterRegistration(self, adapter, owned)

    def list_providers(self) -> list[LlmProviderInfo]:
        """Describe every currently routed provider."""
        return [self._adapters[p].provider_info(p) for p in self._adapters]

    def _adapter(self, provider: str) -> LlmAdapter:
        adapter = self._adapters.get(provider)
        if adapter is None:
            raise LlmError(
                f"no adapter registered for provider {provider!r}",
                code="INVALID_REQUEST",
            )
        return adapter

    async def resolve_model_info(self, provider: str, model: str) -> dict:
        """Resolve one exact model's metadata, checking the adapter's answer."""
        info = await self._adapter(provider).resolve_model(provider, model)
        if not isinstance(info, dict):
            raise LlmError(
                f"adapter returned {type(info).__name__}, expected a dict",
                code="INVALID_REQUEST",
            )
        if info.get("id") != model:
            raise LlmError(
                f"adapter returned model id {info.get('id')!r}, expected {model!r}",
                code="INVALID_REQUEST",
            )
        return info

    # -- streaming -------------------------------------------------------- #

    def _effective_options(self, options: GenerateOptions) -> GenerateOptions:
        """Apply the call-config merge, returning a new options instance."""
        from dataclasses import replace as dc_replace

        merged = merge_call_config(
            self._provider_defaults.get(options.provider),
            None,
            call_config_from_options(options),
        )
        return dc_replace(
            options,
            provider=merged.provider or options.provider,
            model=merged.model or options.model,
            reasoning_effort=merged.reasoning_effort,
            temperature=merged.temperature,
            max_tokens=merged.max_tokens,
            stop=list(merged.stop) if merged.stop is not None else None,
        )

    async def _adapter_stream(
        self, options: GenerateOptions
    ) -> AsyncIterator[StreamChunk]:
        """The innermost stream: the adapter, wrapped in its retry policy.

        Retry is only available *before the first chunk escapes* (invariant
        I5). Once the caller has seen output, re-running the adapter would
        replay that prefix, so the error propagates instead.
        """
        adapter = self._adapter(options.provider)
        policy = self._provider_retry.get(options.provider)
        attempts = 0
        while True:
            emitted = False
            try:
                async for chunk in adapter.stream(options):
                    emitted = True
                    yield chunk
                return
            except LlmError as exc:
                if emitted:
                    raise
                if policy is None or not policy.should_retry(exc.code, attempts):
                    raise
                attempts += 1
                signal = options.signal
                if signal is not None and hasattr(signal, "throw_if_aborted"):
                    signal.throw_if_aborted()
                await asyncio.sleep(policy.delay_for(attempts))

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        """Stream one model call through the ``llm/stream`` waterfall.

        Middleware listeners are called as ``listener(options, next)`` and
        return an async iterable; the innermost ``next`` is the adapter's own
        retrying stream. A listener that never calls ``next`` has legitimately
        replaced the stream.
        """
        effective = self._effective_options(options)

        def inner() -> AsyncIterator[StreamChunk]:
            return self._adapter_stream(effective)

        async for chunk in self.ctx.waterfall(STREAM_WATERFALL, effective, inner):
            yield chunk


__all__ = ["LlmService", "AdapterRegistration", "ADAPTERS_UPDATED", "STREAM_WATERFALL"]
