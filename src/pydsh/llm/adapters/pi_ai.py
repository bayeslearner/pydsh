"""The catalogue adapter — one plugin, many routes, models with capabilities.

Where sprint 17's adapter has a flat provider table, this one resolves each
route against a built-in **catalogue** of models: what each holds, what it can
produce, which modalities it takes, and how it spells reasoning. Config lays
over the catalogue field by field, and anything this build cannot serve is
refused **at mount** with the supported set named.

That last part is the design, not a detail. A narrow table that fails loudly at
mount is a much better failure than a broad one that fails at the first
request, in production, as a provider error nobody can attribute to a config
line written three weeks ago.

The wire is entirely sprint 17's: the same serializer, the same translator, the
same transport seam. Everything here happens above it.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from plugkit import Service

from ...operating.credentials import credential_ref
from ..adapter import LlmAdapter, LlmProviderInfo
from ..attribution import attribution_headers
from ..chunks import GenerateOptions, StreamChunk
from ..errors import LlmError, normalize_api_key
from ..retry import resolve_retry_policy
from .catalogue import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_INPUT,
    DEFAULT_MAX_TOKENS,
    MODALITIES,
    SUPPORTED_PROTOCOLS,
    SUPPORTED_THINKING_FORMATS,
    THINKING_LEVELS,
    catalogue_api,
    catalogue_base_url,
    catalogue_compat,
    catalogue_key_ref,
    catalogue_models,
)
from .openai_compatible import (
    COMPLETIONS_PATH,
    DATA_PREFIX,
    PLACEHOLDER_KEY,
    serialize_messages,
    translate,
)
from .transport import Transport, aborted, resolve_transport, with_idle_timeout

#: How long one read may stall before the stream is declared dead.
DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 300.0

#: The `compat` keys a route or model may set.
COMPAT_KEYS = ("thinking_format", "supports_reasoning_effort")

#: Options the reference carries for pi-ai's own transports. There is no SSE
#: counterpart, so naming one is refused rather than silently ignored — a
#: config line that does nothing is worse than one that is rejected.
UNSERVICEABLE_KEYS = (
    "transport_kind",
    "cache_retention",
    "websocket_connect_timeout_ms",
)


class ProfileError(ValueError):
    """A route this build cannot serve. Raised at mount, never at request."""


def _invalid(provider: str, detail: str) -> None:
    raise ProfileError(f"pi-ai route {provider!r} {detail}")


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def _resolve_reasoning(provider: str, entry: dict, base: Optional[dict]) -> dict:
    """One model's reasoning capability and its level mapping.

    Three shapes, and the distinction between them is load-bearing: a `dict`
    declares exactly what this deployment offers, `False` says the model does
    not reason here, and *omitting* the field inherits the catalogue's answer.
    An empty dict is none of those and is refused rather than guessed at.
    """
    efforts = entry.get("reasoning_efforts", ...)

    if efforts is ...:
        inherited: dict[str, Any] = {"reasoning": bool(base and base.get("reasoning"))}
        if base and isinstance(base.get("reasoning_efforts"), dict):
            inherited["reasoning_efforts"] = dict(base["reasoning_efforts"])
        return inherited

    if efforts is False:
        return {"reasoning": False}

    if not isinstance(efforts, dict) or not efforts:
        _invalid(
            provider,
            f"model {entry['id']!r} has an empty reasoning_efforts; declare the "
            "levels offered, set false for a non-reasoning model, or omit the "
            "field to keep the catalogue's capability",
        )

    for level, wire in efforts.items():
        if level not in THINKING_LEVELS:
            _invalid(
                provider,
                f"model {entry['id']!r} reasoning_efforts names unknown level "
                f"{level!r}; levels are {', '.join(THINKING_LEVELS)}",
            )
        if wire is None:
            if level != "off":
                _invalid(
                    provider,
                    f"model {entry['id']!r} reasoning_efforts.{level} needs the "
                    'wire value to send; only "off" may leave it empty',
                )
        elif not isinstance(wire, str) or not wire:
            _invalid(
                provider,
                f"model {entry['id']!r} reasoning_efforts.{level} must not be empty",
            )

    if not any(level != "off" for level in efforts):
        _invalid(
            provider,
            f"model {entry['id']!r} reasoning_efforts offers no level beyond "
            '"off"; declare a thinking level, or set reasoning_efforts to false',
        )
    return {"reasoning": True, "reasoning_efforts": dict(efforts)}


def _resolve_compat(
    provider: str,
    entry: dict,
    route_compat: Optional[dict],
    provider_compat: Optional[dict],
    base: Optional[dict],
) -> dict:
    """One model's reasoning-dispatch switches, most specific winning.

    Four layers, narrowing: the catalogue's *provider* entry (DeepSeek's
    official endpoint spells reasoning as `thinking`, and that is a fact about
    the provider, not about one model), then the catalogue's model entry, then
    the route's config, then the model's.
    """
    entry_compat = entry.get("compat") or {}
    _check_compat(provider, entry_compat, f"model {entry['id']!r} compat")

    merged = {
        **(provider_compat or {}),
        **((base or {}).get("compat") or {}),
        **(route_compat or {}),
        **entry_compat,
    }
    return {"compat": merged} if merged else {}


def _check_compat(provider: str, compat: Optional[dict], what: str) -> None:
    if not compat:
        return
    for key in compat:
        if key not in COMPAT_KEYS:
            _invalid(
                provider,
                f"{what} names unknown key {key!r}; keys are {', '.join(COMPAT_KEYS)}",
            )
    fmt = compat.get("thinking_format")
    if fmt is not None and fmt not in SUPPORTED_THINKING_FORMATS:
        _invalid(
            provider,
            f"{what}.thinking_format {fmt!r} is not served here; formats are "
            f"{', '.join(SUPPORTED_THINKING_FORMATS)}",
        )


def _check_input(provider: str, modalities: Any, what: str) -> list[str]:
    values = list(modalities or ())
    if not values:
        _invalid(provider, f"{what} must name at least one modality")
    for modality in values:
        if modality not in MODALITIES:
            _invalid(
                provider,
                f"{what} names unknown modality {modality!r}; modalities are "
                f"{', '.join(MODALITIES)}",
            )
    return values


def resolve_route_models(provider: str, route: dict) -> dict:
    """Materialise one route's model table.

    Returns the models *and*, separately, the output ceilings config explicitly
    set. Kept apart on purpose: a catalogue ceiling is what a model **can**
    produce, and only a configured one is what a request **asks** for (I2).
    """
    defaults = catalogue_models(provider)
    catalogue_entry_compat = catalogue_compat(provider)
    configured = route.get("models")
    overrides = route.get("model_overrides") or {}
    default_input = route.get("default_input") or list(DEFAULT_INPUT)

    if configured is not None and overrides:
        _invalid(
            provider,
            "sets both models and model_overrides; overrides apply to the "
            "catalogue's list, so an explicit list makes them unreachable",
        )

    if configured is None:
        if not defaults:
            _invalid(
                provider,
                "is not in the built-in catalogue, so it must declare `models`",
            )
        entries = [dict(model) for model in defaults.values()]
        for model_id, override in overrides.items():
            if model_id not in defaults:
                # Refused, never skipped: a typo in an override that silently
                # does nothing is a configuration that lies about itself.
                _invalid(
                    provider,
                    f"model_overrides names {model_id!r}, which the catalogue does "
                    f"not have (it has {', '.join(sorted(defaults)) or 'none'})",
                )
            for entry in entries:
                if entry["id"] == model_id:
                    entry.update(override)
    else:
        entries = [dict(model) for model in configured]

    models: list[dict] = []
    configured_max_tokens: dict[str, int] = {}
    for entry in entries:
        model_id = entry.get("id")
        if not model_id or not isinstance(model_id, str):
            _invalid(provider, "declares a model with no id")
        base = defaults.get(model_id)

        explicit_max = entry.get("max_tokens")
        if explicit_max is not None and (configured is not None or model_id in overrides):
            configured_max_tokens[model_id] = int(explicit_max)

        resolved: dict[str, Any] = {
            "id": model_id,
            "name": entry.get("name") or (base or {}).get("name") or model_id,
            "context_window": entry.get(
                "context_window",
                (base or {}).get("context_window", DEFAULT_CONTEXT_WINDOW),
            ),
            "max_tokens": entry.get(
                "max_tokens", (base or {}).get("max_tokens", DEFAULT_MAX_TOKENS)
            ),
            "input": _check_input(
                provider,
                entry.get("input") or (base or {}).get("input") or default_input,
                f"model {model_id!r} input",
            ),
        }
        resolved.update(_resolve_reasoning(provider, entry, base))
        resolved.update(
            _resolve_compat(
                provider, entry, route.get("compat"), catalogue_entry_compat, base
            )
        )
        models.append(resolved)

    return {"models": models, "configured_max_tokens": configured_max_tokens}


def resolve_profiles(providers: Any) -> dict[str, dict]:
    """Resolve every configured route. **The only place config is read.**

    :raises ProfileError: anything this build cannot serve, naming the field
        and the supported set (I1).
    """
    if providers is None:
        return {}
    if not isinstance(providers, dict):
        raise ProfileError(
            "pi-ai `providers` is a mapping keyed by route name, not a list"
        )

    resolved: dict[str, dict] = {}
    for provider, source in providers.items():
        if not isinstance(provider, str) or not provider:
            raise ProfileError("pi-ai route names must be non-empty strings")
        if not isinstance(source, dict):
            _invalid(provider, "must be a mapping")

        for key in UNSERVICEABLE_KEYS:
            if source.get(key) is not None:
                _invalid(
                    provider,
                    f"sets {key!r}, which has no counterpart on this SSE wire",
                )

        api = source.get("api") or catalogue_api(provider) or SUPPORTED_PROTOCOLS[0]
        if api not in SUPPORTED_PROTOCOLS:
            _invalid(
                provider,
                f"names api {api!r}, which this build cannot serve; supported "
                f"protocols are {', '.join(SUPPORTED_PROTOCOLS)}",
            )

        base_url = source.get("base_url") or catalogue_base_url(provider)
        if not base_url:
            _invalid(
                provider,
                "is not in the built-in catalogue and declares no base_url",
            )

        reasoning = source.get("reasoning")
        if reasoning is not None and reasoning not in THINKING_LEVELS:
            _invalid(
                provider,
                f"names unknown reasoning level {reasoning!r}; levels are "
                f"{', '.join(THINKING_LEVELS)}",
            )

        route_compat = source.get("compat")
        _check_compat(provider, route_compat, "compat")

        idle = source.get("stream_idle_timeout", DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS)
        if isinstance(idle, bool) or not isinstance(idle, (int, float)) or idle <= 0:
            _invalid(provider, "stream_idle_timeout must be a positive number")

        key_ref = source.get("api_key_ref")
        if key_ref is None:
            key_ref = catalogue_key_ref(provider) or ""
        if key_ref:
            # Validated as a *ref*, and never read here: resolving at mount
            # would freeze a rotated key for the life of the process.
            key_ref = credential_ref(key_ref)

        default_input = _check_input(
            provider, source.get("default_input") or DEFAULT_INPUT, "default_input"
        )

        catalogue = resolve_route_models(
            provider,
            {
                "models": source.get("models"),
                "model_overrides": source.get("model_overrides"),
                "compat": route_compat,
                "default_input": default_input,
            },
        )

        resolved[provider] = {
            "provider": provider,
            "display_name": source.get("display_name") or provider,
            "api": api,
            "base_url": base_url,
            "api_key_ref": key_ref,
            "allow_empty_key": bool(source.get("allow_empty_key", not key_ref)),
            "headers": dict(source["headers"]) if source.get("headers") else None,
            "reasoning": reasoning,
            "stream_idle_timeout": float(idle),
            "retry_policy": resolve_retry_policy(
                source.get("retry_policy"), f"pi-ai route {provider!r} retry_policy"
            ),
            "models": catalogue["models"],
            "configured_max_tokens": catalogue["configured_max_tokens"],
        }
    return resolved


# --------------------------------------------------------------------------- #
# Thinking dispatch
# --------------------------------------------------------------------------- #
def thinking_format_of(model: dict) -> str:
    """How this model spells reasoning. `openai` unless something says otherwise."""
    return (model.get("compat") or {}).get("thinking_format", "openai")


def resolve_wire_reasoning(
    options: Any, model: dict, route_default: Optional[str] = None
) -> dict:
    """The wire fields for one request's reasoning level.

    :raises LlmError: an effort on a model that does not reason (I3) — refused
        here rather than by the endpoint, where it arrives as a generic 400.
    """
    effort = getattr(options, "reasoning_effort", None) or route_default

    if not model.get("reasoning"):
        if effort and effort != "off":
            raise LlmError(
                f"model {model['id']!r} does not support reasoning effort "
                f"{effort!r}",
                "UNSUPPORTED_REASONING_EFFORT",
            )
        return {}

    if not effort or effort == "off":
        if thinking_format_of(model) == "deepseek":
            return {"thinking": {"type": "disabled"}}
        return {}

    declared = model.get("reasoning_efforts")
    wire = declared.get(effort) if isinstance(declared, dict) else None
    if wire is None:
        # The level's own name. Two providers spell "high" differently, which
        # is exactly why the mapping belongs to the model — but a model that
        # declares nothing is assumed to use the ordinary spelling.
        wire = effort

    if thinking_format_of(model) == "deepseek":
        return {"thinking": {"type": "enabled"}, "reasoning_effort": wire}
    return {"reasoning_effort": wire}


def unserializable_blocks(messages: Any) -> list[str]:
    """Block kinds in this request the wire cannot carry (I4).

    The shared serializer joins text blocks and ignores everything else, so a
    block it does not recognise leaves the harness without a word and the
    request goes out silently short. Naming them lets the caller be refused
    instead.
    """
    from ...message import ReasoningBlock, TextBlock, ToolCallBlock, ToolResultBlock

    known = (TextBlock, ReasoningBlock, ToolCallBlock, ToolResultBlock)
    unknown: list[str] = []
    for message in messages or ():
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        if isinstance(content, str):
            continue
        for block in content or ():
            if not isinstance(block, known):
                name = type(block).__name__
                if name not in unknown:
                    unknown.append(name)
    return unknown


def build_wire_request(options: Any, model: dict, profile: dict) -> dict:
    """The `openai-completions` body for one call."""
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
    else:
        # Only what config explicitly set. The catalogue's ceiling is a
        # capability, and using it here would cap every answer at a number
        # nobody chose — invisibly, because the answer simply stops.
        configured = profile["configured_max_tokens"].get(options.model)
        if configured is not None:
            body["max_tokens"] = configured
    if options.stop:
        body["stop"] = list(options.stop)
    body.update(resolve_wire_reasoning(options, model, profile.get("reasoning")))
    return body


def request_headers(profile_headers: Optional[dict]) -> dict:
    """Deployment headers merged with attribution's. Attribution wins (I5).

    A deployment that could overwrite the User-Agent would make this port
    unidentifiable to the provider it is calling, which is the one thing
    attribution exists to prevent.
    """
    attribution = attribution_headers()
    reserved = {name.lower() for name in attribution}
    merged = {
        name: value
        for name, value in (profile_headers or {}).items()
        if name.lower() not in reserved
    }
    merged.update(attribution)
    return merged


# --------------------------------------------------------------------------- #
# The adapter
# --------------------------------------------------------------------------- #
class PiAiAdapter(LlmAdapter):
    """One adapter over many catalogue-resolved routes."""

    def __init__(
        self,
        profiles: Callable[[], dict[str, dict]],
        resolve_api_key: Callable[[str], Awaitable[str]],
        transport: Optional[Transport] = None,
    ) -> None:
        self._profiles = profiles
        self._resolve_api_key = resolve_api_key
        self._transport = resolve_transport(transport)

    # -- metadata ---------------------------------------------------------- #
    def profile_of(self, provider: str) -> dict:
        profile = self._profiles().get(provider)
        if profile is None:
            known = ", ".join(sorted(self._profiles())) or "none"
            raise LlmError(
                f"pi-ai does not serve provider {provider!r} (routes: {known})",
                "NO_ADAPTER",
            )
        return profile

    def model_of(self, profile: dict, model: str) -> dict:
        found = next((m for m in profile["models"] if m["id"] == model), None)
        if found is None:
            known = ", ".join(m["id"] for m in profile["models"]) or "none"
            raise LlmError(
                f"route {profile['provider']!r} has no model {model!r} "
                f"(models: {known})",
                "UNKNOWN_MODEL",
            )
        return found

    def provider_info(self, provider: str) -> LlmProviderInfo:
        try:
            profile = self.profile_of(provider)
        except LlmError:
            return LlmProviderInfo(id=provider, name=provider)
        return LlmProviderInfo(id=provider, name=profile["display_name"])

    @staticmethod
    def supported_efforts(model: dict) -> list[str]:
        """The levels this model offers, ascending. Empty if it does not reason."""
        if not model.get("reasoning"):
            return []
        declared = model.get("reasoning_efforts")
        if isinstance(declared, dict):
            return [level for level in THINKING_LEVELS if level in declared]
        return list(THINKING_LEVELS)

    async def list_models(self, provider: str) -> list[dict]:
        profile = self.profile_of(provider)
        return [
            {
                "provider": provider,
                "id": model["id"],
                "name": model["name"],
                "input_modalities": list(model["input"]),
            }
            for model in profile["models"]
        ]

    async def resolve_model(self, provider: str, model: str) -> dict:
        profile = self.profile_of(provider)
        resolved = self.model_of(profile, model)
        info: dict[str, Any] = {
            "provider": provider,
            "id": model,
            "name": resolved["name"],
            "input_modalities": list(resolved["input"]),
            "context": {"context_window": resolved["context_window"]},
        }
        configured = profile["configured_max_tokens"].get(model)
        if configured is not None:
            info["default_max_tokens"] = configured

        levels = self.supported_efforts(resolved)
        if levels:
            reasoning: dict[str, Any] = {
                "efforts": [{"id": level, "name": level.capitalize()} for level in levels]
            }
            default = profile.get("reasoning")
            if default in levels:
                # Only a level the model actually offers is described as its
                # default; a route-wide setting that this model cannot honour
                # would otherwise be advertised as available.
                reasoning["default_effort"] = default
            info["reasoning"] = reasoning
        return info

    def provider_retry_policy(self, provider: str) -> Any:
        return self._profiles().get(provider, {}).get("retry_policy")

    # -- streaming ---------------------------------------------------------- #
    async def _authorization(self, provider: str, profile: dict) -> str:
        key = self._resolve_api_key(provider)
        if hasattr(key, "__await__"):
            key = await key
        verdict, normalized = normalize_api_key(key or "")
        if verdict == "empty":
            if not profile["allow_empty_key"]:
                raise LlmError(
                    f"route {provider!r} has no API key; set the credential "
                    f"{profile['api_key_ref']!r}",
                    "MISSING_CREDENTIAL",
                )
            normalized = ""
        elif verdict == "illegal":
            raise LlmError(
                f"the API key for route {provider!r} contains characters that "
                "cannot go in a header (printable ASCII only)",
                "ILLEGAL_API_KEY",
            )
        return f"Bearer {normalized or PLACEHOLDER_KEY}"

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        # One snapshot, frozen for the whole request: config changing mid-stream
        # must not move the endpoint out from under a call in flight.
        profile = self.profile_of(options.provider)
        model = self.model_of(profile, options.model)

        unknown = unserializable_blocks(options.messages)
        if unknown:
            raise LlmError(
                f"this route cannot carry {', '.join(unknown)}; the request "
                "would have been sent without it",
                "UNSUPPORTED_CONTENT",
            )

        headers = {
            **request_headers(profile["headers"]),
            "authorization": await self._authorization(options.provider, profile),
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        body = build_wire_request(options, model, profile)
        url = f"{profile['base_url'].rstrip('/')}{COMPLETIONS_PATH}"

        async def payloads() -> AsyncIterator[str]:
            lines = self._transport(url, body, headers, options.signal)
            if hasattr(lines, "__await__"):
                lines = await lines
            bounded = with_idle_timeout(lines, profile["stream_idle_timeout"])
            async for line in bounded:
                if aborted(options.signal):
                    return
                line = line.strip()
                if not line.startswith(DATA_PREFIX):
                    continue
                yield line[len(DATA_PREFIX):].strip()

        async for chunk in translate(payloads()):
            yield chunk


class PiAi(Service):
    """Provides ``ctx.pi_ai`` and registers its routes on ``ctx.llm``."""

    provide = "pi_ai"
    inject = ["llm"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        self._root = getattr(ctx, "root", ctx)
        # Resolved once, here, so an unserviceable config fails at mount with
        # the field named — not at the first request, as a provider error.
        self.profiles = resolve_profiles(config.get("providers"))
        self.adapter = PiAiAdapter(
            lambda: self.profiles, self.api_key_for, config.get("transport")
        )
        if self.profiles:
            release = ctx.llm.register_adapter(list(self.profiles), self.adapter)
            ctx.effect(lambda: release)

    async def api_key_for(self, provider: str) -> str:
        profile = self.adapter.profile_of(provider)
        ref = profile["api_key_ref"]
        if not ref:
            return ""
        credentials = getattr(self._root, "credentials", None)
        if credentials is None:
            if profile["allow_empty_key"]:
                return ""
            raise LlmError(
                f"route {provider!r} needs the credential {ref!r}, but "
                "ctx.credentials is not mounted",
                "MISSING_CREDENTIAL",
            )
        resolved = await credentials.resolve(ref)
        return (resolved or {}).get("value", "")


__all__ = [
    "PiAi",
    "PiAiAdapter",
    "ProfileError",
    "resolve_profiles",
    "resolve_route_models",
    "resolve_wire_reasoning",
    "thinking_format_of",
    "unserializable_blocks",
    "build_wire_request",
    "request_headers",
    "COMPAT_KEYS",
    "UNSERVICEABLE_KEYS",
    "DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS",
]
