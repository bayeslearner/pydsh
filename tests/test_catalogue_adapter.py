"""The catalogue adapter — Requirements 1–4, properties 1–3.

The tests that earn their place are the refusals. A config this build cannot
serve must fail at *mount*, with the field named — because the alternative is
failing at the first request, in production, as an unattributable provider
error.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from plugkit import Context

from pydsh import (
    ChunkType,
    Credentials,
    GenerateOptions,
    LlmError,
    LlmService,
    PiAi,
    ProviderProfileError,
    TextBlock,
    create_user_message,
)
from pydsh.llm.adapters.catalogue import (
    BUILTIN_CATALOGUE,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MAX_TOKENS,
    catalogue_models,
    catalogue_providers,
)
from pydsh.llm.adapters.pi_ai import (
    PiAiAdapter,
    build_wire_request,
    request_headers,
    resolve_profiles,
    resolve_wire_reasoning,
    thinking_format_of,
    unserializable_blocks,
)

pytestmark = pytest.mark.asyncio


def sse(*frames: Any) -> list[str]:
    return [f"data: {json.dumps(f)}" for f in frames] + ["data: [DONE]"]


def frame(**delta: Any) -> dict:
    return {"choices": [{"index": 0, "delta": delta}]}


def finish_frame(reason: str = "stop") -> dict:
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def scripted(lines: list[str], record: Any = None):
    async def transport(url, body, headers, signal=None):
        if record is not None:
            record.append({"url": url, "body": body, "headers": headers})
        for line in lines:
            yield line

    return transport


async def build(providers: dict, transport=None, key: str = "sk-test"):
    root = Context()
    await root.plugin(LlmService)
    await root.plugin(Credentials)
    if key is not None:
        for ref in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"):
            await root.credentials.set(ref, key)
    await root.plugin(
        PiAi,
        {
            "providers": providers,
            "transport": transport or scripted(sse(frame(content="hi"), finish_frame())),
        },
    )
    return root


# --------------------------------------------------------------------------- #
# R1 — the catalogue
# --------------------------------------------------------------------------- #
async def test_the_catalogue_is_readable_without_mounting_anything():
    """R1.4."""
    assert set(catalogue_providers()) == {"openai", "deepseek", "openrouter", "ollama"}


async def test_every_catalogue_model_declares_its_capabilities():
    """R1.2."""
    for provider in catalogue_providers():
        for model in catalogue_models(provider).values():
            assert model["id"] and model["name"]
            assert isinstance(model["context_window"], int)
            assert isinstance(model["max_tokens"], int)


async def test_a_reasoning_model_may_declare_its_level_mapping():
    """R1.3."""
    o3 = catalogue_models("openai")["o3-mini"]
    assert o3["reasoning"] is True
    assert o3["reasoning_efforts"]["high"] == "high"


async def test_reading_the_catalogue_does_not_let_a_caller_edit_it():
    models = catalogue_models("openai")
    models["gpt-4o"]["context_window"] = 1
    assert BUILTIN_CATALOGUE["openai"]["models"][0]["context_window"] == 128_000


# --------------------------------------------------------------------------- #
# R2 — profile resolution
# --------------------------------------------------------------------------- #
async def test_a_catalogue_route_needs_no_config_at_all():
    """R2.2 — the catalogue is the default."""
    profiles = resolve_profiles({"openai": {}})
    profile = profiles["openai"]
    assert profile["base_url"] == "https://api.openai.com/v1"
    assert profile["api_key_ref"] == "OPENAI_API_KEY"
    assert {m["id"] for m in profile["models"]} == {
        "gpt-4o", "gpt-4o-mini", "gpt-4.1", "o3-mini"
    }


async def test_catalogue_capabilities_survive_resolution():
    """Property 1 (R2.2) — empty config changes nothing."""
    for provider in catalogue_providers():
        expected = catalogue_models(provider)
        resolved = {m["id"]: m for m in resolve_profiles({provider: {}})[provider]["models"]}
        assert set(resolved) == set(expected)
        for model_id, model in expected.items():
            assert resolved[model_id]["context_window"] == model["context_window"]
            assert resolved[model_id]["max_tokens"] == model["max_tokens"]
            assert resolved[model_id]["reasoning"] == bool(model.get("reasoning"))


async def test_config_overrides_the_catalogue_field_by_field():
    profiles = resolve_profiles(
        {
            "openai": {
                "base_url": "http://proxy/v1",
                "model_overrides": {"gpt-4o": {"context_window": 64_000}},
            }
        }
    )
    models = {m["id"]: m for m in profiles["openai"]["models"]}
    assert profiles["openai"]["base_url"] == "http://proxy/v1"
    assert models["gpt-4o"]["context_window"] == 64_000
    # Everything the override did not name is kept, not reset.
    assert models["gpt-4o"]["max_tokens"] == 16_384
    assert models["gpt-4o-mini"]["context_window"] == 128_000


async def test_an_unknown_route_is_declarable_entirely_from_config():
    """R2.3."""
    profiles = resolve_profiles(
        {
            "acme": {
                "base_url": "https://acme.example/v1",
                "api_key_ref": "ACME_KEY",
                "models": [{"id": "acme-1", "name": "Acme One"}],
            }
        }
    )
    model = profiles["acme"]["models"][0]
    assert model["context_window"] == DEFAULT_CONTEXT_WINDOW
    assert model["max_tokens"] == DEFAULT_MAX_TOKENS
    assert model["reasoning"] is False


async def test_an_unknown_route_without_a_base_url_is_refused():
    with pytest.raises(ProviderProfileError) as caught:
        resolve_profiles({"acme": {"models": [{"id": "x"}]}})
    assert "base_url" in str(caught.value)


async def test_an_unknown_route_without_models_is_refused():
    with pytest.raises(ProviderProfileError) as caught:
        resolve_profiles({"acme": {"base_url": "https://acme/v1"}})
    assert "models" in str(caught.value)


@pytest.mark.parametrize(
    "config,expected",
    [
        ({"api": "anthropic-messages"}, "openai-completions"),
        ({"compat": {"thinking_format": "zai"}}, "openai, deepseek"),
        ({"compat": {"wibble": True}}, "thinking_format"),
        ({"reasoning": "extreme"}, "off, minimal"),
        ({"default_input": ["audio"]}, "text, image"),
        ({"cache_retention": "long"}, "no counterpart"),
        ({"stream_idle_timeout": 0}, "positive"),
        ({"stream_idle_timeout": -1}, "positive"),
    ],
)
async def test_nothing_unsupported_is_accepted(config, expected):
    """Property 2 (R2.4, I1) — refused at mount, with the supported set named."""
    with pytest.raises(ProviderProfileError) as caught:
        resolve_profiles({"openai": config})
    assert expected in str(caught.value)


async def test_the_refusal_names_the_route():
    with pytest.raises(ProviderProfileError) as caught:
        resolve_profiles({"my-proxy": {"base_url": "http://x/v1", "api": "grpc"}})
    assert "my-proxy" in str(caught.value)


async def test_an_override_naming_an_unknown_model_is_refused_not_skipped():
    """R2.5 — a typo that silently does nothing is a config that lies."""
    with pytest.raises(ProviderProfileError) as caught:
        resolve_profiles({"openai": {"model_overrides": {"gpt-5o": {"max_tokens": 1}}}})
    assert "gpt-5o" in str(caught.value)


async def test_models_and_overrides_together_are_refused():
    with pytest.raises(ProviderProfileError) as caught:
        resolve_profiles(
            {
                "openai": {
                    "models": [{"id": "x"}],
                    "model_overrides": {"gpt-4o": {"max_tokens": 1}},
                }
            }
        )
    assert "unreachable" in str(caught.value)


@pytest.mark.parametrize(
    "efforts,expected",
    [
        ({}, "empty reasoning_efforts"),
        ({"wat": "x"}, "unknown level"),
        ({"high": None}, "needs the"),
        ({"high": ""}, "must not be empty"),
        ({"off": None}, "no level beyond"),
    ],
)
async def test_a_bad_reasoning_effort_table_is_refused(efforts, expected):
    """R2.6, R2.7."""
    with pytest.raises(ProviderProfileError) as caught:
        resolve_profiles(
            {
                "acme": {
                    "base_url": "https://acme/v1",
                    "models": [{"id": "a", "reasoning_efforts": efforts}],
                }
            }
        )
    assert expected in str(caught.value)


async def test_reasoning_efforts_false_says_this_model_does_not_reason():
    profiles = resolve_profiles(
        {"openai": {"model_overrides": {"o3-mini": {"reasoning_efforts": False}}}}
    )
    o3 = next(m for m in profiles["openai"]["models"] if m["id"] == "o3-mini")
    assert o3["reasoning"] is False


async def test_omitting_reasoning_efforts_inherits_the_catalogue():
    """The three shapes are different answers, not one with defaults."""
    profiles = resolve_profiles({"openai": {}})
    o3 = next(m for m in profiles["openai"]["models"] if m["id"] == "o3-mini")
    assert o3["reasoning"] is True
    assert o3["reasoning_efforts"]["medium"] == "medium"


async def test_a_bad_credential_ref_is_refused_at_mount():
    """R2.8 — validated as a ref, and not read."""
    with pytest.raises(Exception) as caught:
        resolve_profiles({"acme": {"base_url": "http://x/v1", "api_key_ref": "not a ref!", "models": [{"id": "a"}]}})
    assert "ref" in str(caught.value).lower()


async def test_no_credential_is_read_during_resolution(monkeypatch):
    """R2.8 — resolution touches no secret at all."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    profile = resolve_profiles({"openai": {}})["openai"]
    assert profile["api_key_ref"] == "OPENAI_API_KEY"
    assert "sk-from-env" not in json.dumps(profile, default=str)


async def test_a_catalogue_ceiling_is_not_a_request_default():
    """Property 3 (R2.9, I2) — the invisible cap this prevents."""
    profile = resolve_profiles({"openai": {}})["openai"]
    assert profile["configured_max_tokens"] == {}

    model = next(m for m in profile["models"] if m["id"] == "gpt-4o")
    body = build_wire_request(
        GenerateOptions(provider="openai", model="gpt-4o", messages=[]), model, profile
    )
    assert "max_tokens" not in body


async def test_a_configured_ceiling_does_become_a_request_default():
    profile = resolve_profiles(
        {"openai": {"model_overrides": {"gpt-4o": {"max_tokens": 512}}}}
    )["openai"]
    assert profile["configured_max_tokens"] == {"gpt-4o": 512}

    model = next(m for m in profile["models"] if m["id"] == "gpt-4o")
    body = build_wire_request(
        GenerateOptions(provider="openai", model="gpt-4o", messages=[]), model, profile
    )
    assert body["max_tokens"] == 512


async def test_a_request_max_tokens_beats_the_configured_default():
    profile = resolve_profiles(
        {"openai": {"model_overrides": {"gpt-4o": {"max_tokens": 512}}}}
    )["openai"]
    model = next(m for m in profile["models"] if m["id"] == "gpt-4o")
    body = build_wire_request(
        GenerateOptions(provider="openai", model="gpt-4o", messages=[], max_tokens=64),
        model,
        profile,
    )
    assert body["max_tokens"] == 64


async def test_no_providers_resolves_to_nothing():
    assert resolve_profiles(None) == {}
    assert resolve_profiles({}) == {}


async def test_a_list_of_providers_is_refused():
    with pytest.raises(ProviderProfileError) as caught:
        resolve_profiles([{"provider": "openai"}])
    assert "mapping" in str(caught.value)


# --------------------------------------------------------------------------- #
# R3 — thinking dispatch
# --------------------------------------------------------------------------- #
def reasoning_model(**extra: Any) -> dict:
    return {"id": "m", "name": "M", "reasoning": True, **extra}


async def test_openai_format_sends_reasoning_effort_only():
    """R3.1."""
    options = GenerateOptions(provider="p", model="m", messages=[], reasoning_effort="high")
    assert resolve_wire_reasoning(options, reasoning_model()) == {"reasoning_effort": "high"}


async def test_openai_format_sends_nothing_for_off():
    options = GenerateOptions(provider="p", model="m", messages=[], reasoning_effort="off")
    assert resolve_wire_reasoning(options, reasoning_model()) == {}


async def test_deepseek_format_disables_thinking_for_off():
    """R3.2."""
    model = reasoning_model(compat={"thinking_format": "deepseek"})
    options = GenerateOptions(provider="p", model="m", messages=[], reasoning_effort="off")
    assert resolve_wire_reasoning(options, model) == {"thinking": {"type": "disabled"}}


async def test_deepseek_format_enables_thinking_with_an_effort():
    model = reasoning_model(compat={"thinking_format": "deepseek"})
    options = GenerateOptions(provider="p", model="m", messages=[], reasoning_effort="high")
    assert resolve_wire_reasoning(options, model) == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }


async def test_a_models_own_mapping_is_preferred_over_the_level_name():
    """R3.3 — two providers spell "high" differently."""
    model = reasoning_model(reasoning_efforts={"off": None, "high": "ultra"})
    options = GenerateOptions(provider="p", model="m", messages=[], reasoning_effort="high")
    assert resolve_wire_reasoning(options, model) == {"reasoning_effort": "ultra"}


async def test_an_undeclared_level_falls_back_to_its_own_name():
    model = reasoning_model(reasoning_efforts={"off": None, "low": "l"})
    options = GenerateOptions(provider="p", model="m", messages=[], reasoning_effort="max")
    assert resolve_wire_reasoning(options, model) == {"reasoning_effort": "max"}


async def test_reasoning_on_a_non_reasoning_model_is_refused_here():
    """R3.4, I3 — not by the endpoint, where it is a generic 400."""
    model = {"id": "m", "name": "M", "reasoning": False}
    options = GenerateOptions(provider="p", model="m", messages=[], reasoning_effort="high")
    with pytest.raises(LlmError) as caught:
        resolve_wire_reasoning(options, model)
    assert caught.value.code == "UNSUPPORTED_REASONING_EFFORT"


async def test_off_on_a_non_reasoning_model_is_fine():
    model = {"id": "m", "name": "M", "reasoning": False}
    options = GenerateOptions(provider="p", model="m", messages=[], reasoning_effort="off")
    assert resolve_wire_reasoning(options, model) == {}


async def test_a_request_effort_overrides_the_routes_default():
    """R3.5."""
    model = reasoning_model()
    options = GenerateOptions(provider="p", model="m", messages=[], reasoning_effort="low")
    assert resolve_wire_reasoning(options, model, "high") == {"reasoning_effort": "low"}

    bare = GenerateOptions(provider="p", model="m", messages=[])
    assert resolve_wire_reasoning(bare, model, "high") == {"reasoning_effort": "high"}


async def test_the_deepseek_route_inherits_its_thinking_format():
    profile = resolve_profiles({"deepseek": {}})["deepseek"]
    model = next(m for m in profile["models"] if m["id"] == "deepseek-reasoner")
    assert thinking_format_of(model) == "deepseek"


# --------------------------------------------------------------------------- #
# R4 — the adapter
# --------------------------------------------------------------------------- #
async def test_list_models_reports_modalities():
    """R4.1."""
    root = await build({"openai": {}})
    listed = await root.pi_ai.adapter.list_models("openai")
    assert {m["id"] for m in listed} == {"gpt-4o", "gpt-4o-mini", "gpt-4.1", "o3-mini"}
    assert listed[0]["input_modalities"] == ["text"]


async def test_resolve_model_reports_the_context_window_and_efforts():
    """R4.2."""
    root = await build({"openai": {"reasoning": "medium"}})
    info = await root.llm.resolve_model_info("openai", "o3-mini")
    assert info["context"]["context_window"] == 200_000
    assert [e["id"] for e in info["reasoning"]["efforts"]] == ["off", "low", "medium", "high"]
    assert info["reasoning"]["default_effort"] == "medium"


async def test_a_route_default_the_model_cannot_offer_is_not_advertised():
    """A bad route-wide setting must not misdescribe a model's options."""
    root = await build({"openai": {"reasoning": "xhigh"}})
    info = await root.llm.resolve_model_info("openai", "o3-mini")
    assert "default_effort" not in info["reasoning"]


async def test_a_non_reasoning_model_offers_no_reasoning_control():
    root = await build({"openai": {}})
    info = await root.llm.resolve_model_info("openai", "gpt-4o")
    assert "reasoning" not in info


async def test_an_unknown_model_is_refused_by_name():
    """R4.3."""
    root = await build({"openai": {}})
    with pytest.raises(LlmError) as caught:
        async for _ in root.llm.stream(
            GenerateOptions(provider="openai", model="gpt-9", messages=[])
        ):
            pass
    assert caught.value.code == "UNKNOWN_MODEL"
    assert "gpt-9" in str(caught.value)


async def test_an_unserializable_block_is_refused_before_the_request():
    """R4.4, I4 — the serializer would have dropped it silently."""

    class Mystery:
        type = "video"

    assert unserializable_blocks(
        [create_user_message([TextBlock("hi")])]
    ) == []

    class FakeMessage:
        role = "user"
        content = (Mystery(),)

    assert unserializable_blocks([FakeMessage()]) == ["Mystery"]

    sent: list = []
    root = await build({"openai": {}}, transport=scripted(sse(finish_frame()), sent))
    with pytest.raises(LlmError) as caught:
        async for _ in root.llm.stream(
            GenerateOptions(provider="openai", model="gpt-4o", messages=[FakeMessage()])
        ):
            pass
    assert caught.value.code == "UNSUPPORTED_CONTENT"
    assert sent == []


async def test_attribution_headers_win_over_deployment_headers():
    """R4.5, I5."""
    merged = request_headers({"User-Agent": "not-us", "x-team": "ours"})
    assert merged["x-team"] == "ours"
    assert merged["user-agent"] != "not-us"
    assert "User-Agent" not in merged


async def test_streaming_reuses_the_shared_wire():
    """R4.6."""
    sent: list = []
    root = await build(
        {"openai": {"headers": {"x-team": "ours"}}},
        transport=scripted(sse(frame(content="hello"), finish_frame()), sent),
    )
    chunks = [
        c
        async for c in root.llm.stream(
            GenerateOptions(provider="openai", model="gpt-4o", messages=[])
        )
    ]
    assert chunks[-1].finish == {"kind": "stop"}
    assert sent[0]["url"] == "https://api.openai.com/v1/chat/completions"
    assert sent[0]["headers"]["x-team"] == "ours"
    assert sent[0]["headers"]["authorization"] == "Bearer sk-test"
    assert sent[0]["body"]["stream"] is True


async def test_each_route_carries_its_own_retry_policy():
    """R4.7."""
    root = await build(
        {
            "openai": {"retry_policy": {"maxRetries": 5}},
            "ollama": {},
        }
    )
    assert root.pi_ai.adapter.provider_retry_policy("openai").max_retries == 5
    assert root.pi_ai.adapter.provider_retry_policy("ollama").max_retries != 5


async def test_a_route_with_no_credential_needs_none():
    root = await build({"ollama": {}}, key=None)
    chunks = [
        c
        async for c in root.llm.stream(
            GenerateOptions(provider="ollama", model="llama3.1:8b", messages=[])
        )
    ]
    assert chunks[-1].finish == {"kind": "stop"}


async def test_an_unowned_provider_raises_no_adapter():
    root = await build({"openai": {}})
    with pytest.raises(LlmError) as caught:
        root.pi_ai.adapter.profile_of("nowhere")
    assert caught.value.code == "NO_ADAPTER"


async def test_mounting_with_no_routes_registers_nothing():
    root = Context()
    await root.plugin(LlmService)
    await root.plugin(PiAi, {})
    assert root.llm.list_providers() == []


async def test_an_unserviceable_config_fails_at_mount_not_at_request():
    """I1 — the whole point of resolving once, up front."""
    root = Context()
    await root.plugin(LlmService)
    with pytest.raises(ProviderProfileError):
        await root.plugin(PiAi, {"providers": {"openai": {"api": "anthropic-messages"}}})
