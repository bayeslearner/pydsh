"""``ctx.system_prompt`` — the system prompt as a registry, not a string.

Four registries, each contributed to by plugins that never see the whole:

- **sections** — the prompt itself, ordered. Lower ``order`` comes first.
- **contexts** — dynamic runtime snapshots, rendered here but delivered as
  conversation history rather than prompt.
- **variables** — named values sections interpolate with ``{{name}}``.
- **tool providers** — schema sources, presented in a configurable order.

Every registration returns a disposer, and the disposer is **identity-guarded**:
it removes the entry only if that entry is still the one it registered. Stale
handles are normal in a plugin system — a fiber unloads after another has taken
the name over — and a disposer that deleted whatever was under the name would
quietly remove a live registration.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from plugkit import Service

from .interpolate import VARIABLE_NAME, interpolate
from .ordering import TOOL_ORDER_REST, order_tools
from .sections import PromptAssembly, PromptContext, PromptSection, resolve_text

#: The waterfall a plugin hooks to transform a whole assembly.
ASSEMBLE_WATERFALL = "system-prompt/assemble"

#: The harness's own identity, first in the prompt unless switched off.
HARNESS_IDENTITY_SECTION = "harness:identity"
HARNESS_IDENTITY_ORDER = -100
HARNESS_IDENTITY_TEXT = "You are an AI agent powered by DeepSeek Harness."

#: The deployment's persona, at the origin — everything else orders around it.
PERSONA_SECTION = "deployment:persona"
PERSONA_ORDER = 0

#: Prefixes a rendered runtime-context snapshot. The supersession sentence is
#: load-bearing: several snapshots accumulate in history over a conversation,
#: and without it the model weighs a stale one equally with the current one.
CONTEXT_SNAPSHOT_HEADER = (
    "Current runtime context. This snapshot supersedes earlier "
    "runtime-context snapshots."
)


class PromptRegistrationError(ValueError):
    """A registration conflicts with one already in place."""


class _Entry:
    """A registration plus the sequence number that breaks order ties."""

    __slots__ = ("value", "seq")

    def __init__(self, value: Any, seq: int) -> None:
        self.value = value
        self.seq = seq


class SystemPrompt(Service):
    """Provides ``ctx.system_prompt``."""

    provide = "system_prompt"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        # Same reason the agent loop does it: plugkit's `inject` is also the
        # permission list, so an optional service is unreadable from a plugin
        # context that did not declare it. See `pydsh.agent.agent.Agent`.
        self._root = getattr(ctx, "root", ctx)
        self._sections: dict[str, _Entry] = {}
        self._contexts: dict[str, _Entry] = {}
        self._variables: dict[str, _Entry] = {}
        self._tool_providers: list[_Entry] = []
        self._seq = 0
        # Counted, not a flag: two plugins may each want runtime context
        # suppressed, and the first to release must not un-suppress for both.
        self._suppressions = 0

        self._tool_order: Optional[list[str]] = None
        tool_order = config.get("tool_order")
        if tool_order is not None:
            if TOOL_ORDER_REST not in tool_order:
                raise ValueError(
                    f"tool_order must contain the rest marker {TOOL_ORDER_REST!r} — "
                    "it is where tools you did not name are inserted, and without "
                    "it a newly registered tool would silently never be shown"
                )
            self._tool_order = list(tool_order)

        if config.get("include_harness_identity", True):
            self.section(
                PromptSection(
                    name=HARNESS_IDENTITY_SECTION,
                    order=HARNESS_IDENTITY_ORDER,
                    text=HARNESS_IDENTITY_TEXT,
                )
            )
        # Registered even when empty, so a deployment can rely on the slot
        # existing at order 0. Rendering drops it when it has no text.
        self.section(
            PromptSection(
                name=PERSONA_SECTION,
                order=PERSONA_ORDER,
                text=config.get("persona") or "",
            )
        )
        if config.get("include_registered_tools", True):
            self.tools(self._registered_tool_schemas)
        if not config.get("include_runtime_context", True):
            self.suppress_runtime_context()

    def _registered_tool_schemas(self, context: dict) -> dict:
        """The tools mounted on ``ctx.tools``, as schemas the model can read.

        The reference registers no such provider, so its assembly's tool list
        is always empty and its ``toolOrder`` config controls nothing — the
        loop reads the registry directly and never consults the assembly. That
        is an orphaned capability, so this port bridges the two: registered
        tools flow through the assembly, which is what makes ``tool_order`` and
        the ``system-prompt/assemble`` waterfall able to reach them at all.
        """
        tools = getattr(self._root, "tools", None)
        if tools is None:
            return {"schemas": []}
        return {
            "schemas": [
                {
                    "name": tool.name,
                    "description": getattr(tool, "description", "") or "",
                    "parameters": getattr(tool, "parameters", None) or {},
                }
                for tool in tools.list()
            ]
        }

    # -- registration ------------------------------------------------------ #
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _remove(self, registry: dict[str, _Entry], key: str, entry: _Entry) -> bool:
        """Remove ``key`` only if it still holds ``entry`` (I1)."""
        if registry.get(key) is entry:
            del registry[key]
            return True
        return False

    def section(self, section: PromptSection) -> Callable[[], bool]:
        """Register an ordered piece of the prompt; returns a disposer."""
        order = section.order
        if isinstance(order, bool) or not isinstance(order, (int, float)):
            raise TypeError(
                f"prompt section {section.name!r} needs a numeric order, got {order!r}"
            )
        if order != order:  # NaN sorts unpredictably against everything
            raise ValueError(f"prompt section {section.name!r} has a NaN order")
        if section.name in self._sections:
            raise PromptRegistrationError(
                f"prompt section {section.name!r} is already registered"
            )
        entry = _Entry(section, self._next_seq())
        self._sections[section.name] = entry
        return lambda: self._remove(self._sections, section.name, entry)

    def context(self, context: PromptContext) -> Callable[[], bool]:
        """Register a runtime-context contributor; returns a disposer."""
        if context.name in self._contexts:
            raise PromptRegistrationError(
                f"prompt context {context.name!r} is already registered"
            )
        entry = _Entry(context, self._next_seq())
        self._contexts[context.name] = entry
        return lambda: self._remove(self._contexts, context.name, entry)

    def variable(
        self, name: str, provider: Callable[[dict], Optional[str]]
    ) -> Callable[[], bool]:
        """Register a ``{{name}}`` provider; returns a disposer."""
        if not VARIABLE_NAME.fullmatch(name):
            raise ValueError(
                f"illegal prompt variable name {name!r}: "
                f"a name must match {VARIABLE_NAME.pattern}"
            )
        if name in self._variables:
            raise PromptRegistrationError(
                f"prompt variable {name!r} is already registered"
            )
        entry = _Entry(provider, self._next_seq())
        self._variables[name] = entry
        return lambda: self._remove(self._variables, name, entry)

    def tools(self, provider: Callable[[dict], dict]) -> Callable[[], bool]:
        """Register a tool-schema provider returning ``{"schemas": [...]}``."""
        entry = _Entry(provider, self._next_seq())
        self._tool_providers.append(entry)

        def dispose() -> bool:
            # By identity: the same callable may be registered twice, and
            # `list.remove` would drop whichever compared equal first.
            for index, candidate in enumerate(self._tool_providers):
                if candidate is entry:
                    self._tool_providers.pop(index)
                    return True
            return False

        return dispose

    def suppress_runtime_context(self) -> Callable[[], bool]:
        """Suppress runtime context; returns a release, idempotent per handle."""
        self._suppressions += 1
        released = False

        def release() -> bool:
            nonlocal released
            if released:
                return False
            released = True
            self._suppressions -= 1
            return True

        return release

    @property
    def runtime_context_suppressed(self) -> bool:
        """Whether any holder is currently suppressing runtime context."""
        return self._suppressions > 0

    # -- assembly ---------------------------------------------------------- #
    def _ordered(self, registry: dict[str, _Entry]) -> list[Any]:
        """Registered values by ascending order, ties broken by registration.

        The tie-break is explicit rather than left to dict ordering, so two
        sections at the same order have a documented arrangement instead of one
        a reader has to infer from the implementation.
        """
        return [
            entry.value
            for entry in sorted(
                registry.values(), key=lambda e: (e.value.order, e.seq)
            )
        ]

    async def assemble(self, context: Optional[dict] = None) -> PromptAssembly:
        """Resolve every registry into one assembly and let plugins transform it."""
        context = context or {}

        variables: dict[str, Optional[str]] = {
            name: entry.value(context) for name, entry in self._variables.items()
        }

        registered_sections = self._ordered(self._sections)
        complete = [s for s in registered_sections if s.complete]
        if len(complete) > 1:
            raise PromptRegistrationError(
                "more than one complete prompt section is in effect: "
                + ", ".join(s.name for s in complete)
                + " — a complete section replaces the whole prompt, so two "
                "cannot both apply"
            )

        sections = [
            {"name": s.name, "text": resolve_text(s.text, context)}
            for s in registered_sections
        ]
        complete_section = (
            {"name": complete[0].name, "text": resolve_text(complete[0].text, context)}
            if complete
            else None
        )

        contexts: list[dict] = []
        if not self.runtime_context_suppressed:
            contexts = [
                {"name": c.name, "text": resolve_text(c.text, context)}
                for c in self._ordered(self._contexts)
            ]

        tools: list[dict] = []
        seen: set[str] = set()
        for entry in list(self._tool_providers):
            for schema in entry.value(context).get("schemas", []):
                name = schema.get("name")
                if name not in seen:
                    seen.add(name)
                    tools.append(dict(schema))
        tools = order_tools(tools, self._tool_order)

        assembly = PromptAssembly(
            sections=sections, contexts=contexts, tools=tools, variables=variables
        )

        async def unchanged() -> PromptAssembly:
            return assembly

        transformed = await self.ctx.waterfall(
            ASSEMBLE_WATERFALL, assembly, context, unchanged
        )
        if not isinstance(transformed, PromptAssembly):
            transformed = assembly

        # A complete section and a suppression are decisions of *this* service,
        # not suggestions for listeners: reasserting them after the waterfall is
        # what makes them guarantees rather than defaults a listener can undo.
        if complete_section is None and not self.runtime_context_suppressed:
            return transformed
        return PromptAssembly(
            sections=[complete_section] if complete_section else transformed.sections,
            contexts=[] if self.runtime_context_suppressed else transformed.contexts,
            tools=transformed.tools,
            variables=transformed.variables,
        )

    # -- rendering --------------------------------------------------------- #
    @staticmethod
    def render_prompt(assembly: PromptAssembly) -> str:
        """The final system prompt: interpolate, drop empties, join."""
        parts = [
            rendered
            for section in assembly.sections
            if (
                rendered := interpolate(
                    section["name"], section["text"], assembly.variables
                )
            )
        ]
        return "\n\n".join(parts)

    @staticmethod
    def render_context_sections(assembly: PromptAssembly) -> list[dict]:
        """Each non-empty runtime context, interpolated and named."""
        out = []
        for entry in assembly.contexts:
            rendered = interpolate(
                entry["name"], entry["text"], assembly.variables, kind="context"
            )
            if rendered:
                out.append({"name": entry["name"], "text": rendered})
        return out

    @classmethod
    def render_context_snapshot(cls, assembly: PromptAssembly) -> str:
        """The whole runtime snapshot, or ``""`` when nothing contributed."""
        body = "\n\n".join(s["text"] for s in cls.render_context_sections(assembly))
        return f"{CONTEXT_SNAPSHOT_HEADER}\n\n{body}" if body else ""


__all__ = [
    "SystemPrompt",
    "PromptRegistrationError",
    "ASSEMBLE_WATERFALL",
    "HARNESS_IDENTITY_SECTION",
    "PERSONA_SECTION",
    "PERSONA_ORDER",
    "CONTEXT_SNAPSHOT_HEADER",
]
