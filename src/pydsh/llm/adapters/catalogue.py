"""The built-in provider catalogue — models with capabilities, not just names.

Sprint 17's provider table knows where a provider lives. This one knows what
its *models* can do: how much context each holds, how much it can produce,
which modalities it accepts, and whether it reasons. That matters downstream —
compaction budgets against a context window, and a `/model` listing has
something to list.

Every number here is a **capability**, never a default. `max_tokens` says what
a model can produce; what a request asks for is a separate decision, and
conflating the two caps every answer at a number nobody chose.

This is a *representative* catalogue, not an exhaustive one. A deployment
overrides it field by field, or declares a provider it does not contain at all.
Pretending to enumerate every model of every vendor would be a maintenance
promise this repo cannot keep — and a stale entry is worse than an absent one,
because it looks authoritative.
"""

from __future__ import annotations

from typing import Any, Optional

#: The wire protocols this build can serve. Deliberately one: a config naming
#: another is refused at mount with this list, rather than accepted and
#: discovered to be unserviceable at the first request.
SUPPORTED_PROTOCOLS: tuple[str, ...] = ("openai-completions",)

#: How a provider spells reasoning on the wire.
SUPPORTED_THINKING_FORMATS: tuple[str, ...] = ("openai", "deepseek")

#: Reasoning levels, ascending. A model declares which of these it offers and
#: what each one is called on its own wire.
THINKING_LEVELS: tuple[str, ...] = (
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

#: Input kinds a model may accept. This build's wire path carries text.
MODALITIES: tuple[str, ...] = ("text", "image")

#: What a model accepts when it says nothing.
DEFAULT_INPUT: tuple[str, ...] = ("text",)

#: Context a model holds when neither the catalogue nor config says.
DEFAULT_CONTEXT_WINDOW = 262_144

#: What a model can produce when neither says. A ceiling, not a default.
DEFAULT_MAX_TOKENS = 32_768

#: The built-in table. Capacities are from each vendor's published
#: documentation at the time of writing; a deployment that finds one stale
#: overrides it rather than waiting for this file to change.
BUILTIN_CATALOGUE: dict[str, dict] = {
    "openai": {
        "api": "openai-completions",
        "base_url": "https://api.openai.com/v1",
        "api_key_ref": "OPENAI_API_KEY",
        "models": [
            {"id": "gpt-4o", "name": "GPT-4o", "context_window": 128_000, "max_tokens": 16_384},
            {"id": "gpt-4o-mini", "name": "GPT-4o mini", "context_window": 128_000, "max_tokens": 16_384},
            {"id": "gpt-4.1", "name": "GPT-4.1", "context_window": 1_047_576, "max_tokens": 32_768},
            {
                "id": "o3-mini",
                "name": "o3-mini",
                "context_window": 200_000,
                "max_tokens": 100_000,
                "reasoning": True,
                "reasoning_efforts": {
                    "off": None,
                    "low": "low",
                    "medium": "medium",
                    "high": "high",
                },
            },
        ],
    },
    "deepseek": {
        "api": "openai-completions",
        "base_url": "https://api.deepseek.com",
        "api_key_ref": "DEEPSEEK_API_KEY",
        # The official endpoint spells reasoning as a `thinking` structure
        # rather than a bare `reasoning_effort`.
        "compat": {"thinking_format": "deepseek"},
        "models": [
            {
                "id": "deepseek-chat",
                "name": "DeepSeek Chat",
                "context_window": 1_000_000,
                "max_tokens": 256_000,
            },
            {
                "id": "deepseek-reasoner",
                "name": "DeepSeek Reasoner",
                "context_window": 1_000_000,
                "max_tokens": 256_000,
                "reasoning": True,
            },
        ],
    },
    "openrouter": {
        "api": "openai-completions",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_ref": "OPENROUTER_API_KEY",
        "models": [
            {
                "id": "openrouter/auto",
                "name": "OpenRouter Auto",
                "context_window": 128_000,
                "max_tokens": 16_384,
            },
        ],
    },
    "ollama": {
        "api": "openai-completions",
        "base_url": "http://localhost:11434/v1",
        "api_key_ref": "",
        "models": [
            {"id": "llama3.1:8b", "name": "Llama 3.1 8B", "context_window": 128_000, "max_tokens": 8_192},
            {"id": "qwen2.5:7b", "name": "Qwen 2.5 7B", "context_window": 32_768, "max_tokens": 8_192},
        ],
    },
}


def catalogue_providers() -> list[str]:
    """Every provider the built-in catalogue knows."""
    return list(BUILTIN_CATALOGUE)


def catalogue_entry(provider: str) -> Optional[dict]:
    """One provider's catalogue entry, or ``None``."""
    return BUILTIN_CATALOGUE.get(provider)


def catalogue_models(provider: str) -> dict[str, dict]:
    """A provider's models, by id. Copies, so a caller cannot edit the table."""
    entry = BUILTIN_CATALOGUE.get(provider) or {}
    return {model["id"]: dict(model) for model in entry.get("models", ())}


def catalogue_base_url(provider: str) -> Optional[str]:
    return (BUILTIN_CATALOGUE.get(provider) or {}).get("base_url")


def catalogue_api(provider: str) -> Optional[str]:
    return (BUILTIN_CATALOGUE.get(provider) or {}).get("api")


def catalogue_compat(provider: str) -> Optional[dict]:
    compat = (BUILTIN_CATALOGUE.get(provider) or {}).get("compat")
    return dict(compat) if compat else None


def catalogue_key_ref(provider: str) -> Optional[str]:
    return (BUILTIN_CATALOGUE.get(provider) or {}).get("api_key_ref")


__all__ = [
    "BUILTIN_CATALOGUE",
    "catalogue_providers",
    "catalogue_entry",
    "catalogue_models",
    "catalogue_base_url",
    "catalogue_api",
    "catalogue_compat",
    "catalogue_key_ref",
    "SUPPORTED_PROTOCOLS",
    "SUPPORTED_THINKING_FORMATS",
    "THINKING_LEVELS",
    "MODALITIES",
    "DEFAULT_INPUT",
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_MAX_TOKENS",
]
