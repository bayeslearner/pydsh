"""The provider adapter contract.

An adapter is the **only** place transport lives (invariant I1). Core imports
no HTTP client; it calls :meth:`LlmAdapter.stream` and consumes chunks.

Only ``stream`` is abstract. The discovery methods have workable defaults so a
minimal adapter — including a test fake — is a single method.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterable

from .chunks import GenerateOptions, StreamChunk


@dataclass(frozen=True)
class LlmProviderInfo:
    """Provider metadata as the seam reports it."""

    id: str
    name: str


class LlmAdapter(ABC):
    """A provider backend."""

    def provider_info(self, provider: str) -> LlmProviderInfo:
        """Describe one of this adapter's provider routes."""
        return LlmProviderInfo(id=provider, name=provider)

    async def list_models(self, provider: str) -> list[dict]:
        """Models this route can serve; empty when the adapter cannot say."""
        return []

    async def resolve_model(self, provider: str, model: str) -> dict:
        """Full metadata for one exact model.

        An adapter may add capability metadata — notably
        ``context: {"context_window": N}``, which compaction budgets against.
        The returned ``id`` must equal the requested model; the seam enforces it.
        """
        return {"provider": provider, "id": model, "name": model}

    @abstractmethod
    def stream(self, options: GenerateOptions) -> AsyncIterable[StreamChunk]:
        """Yield raw chunks for one model call.

        Must honour ``options.signal`` for cancellation, and must raise
        :class:`~pydsh.llm.errors.LlmError` with a stable code rather than a
        provider-native exception — the retry policy decides on that code.
        """
        raise NotImplementedError


__all__ = ["LlmAdapter", "LlmProviderInfo"]
