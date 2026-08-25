"""The prompt registry on a real kernel — Requirements 1, 2, 4, 5.

The two properties worth the most here are about *scoping*: a disposer must
remove only its own registration, and a suppression must survive one of its
holders releasing. Both are defects in the reference, and both are the kind
that only show up once several plugins load and unload independently.
"""

from __future__ import annotations

import pytest

from plugkit import Context, PointsService, ToolsService

from pydsh.prompt import (
    ASSEMBLE_WATERFALL,
    CONTEXT_SNAPSHOT_HEADER,
    HARNESS_IDENTITY_SECTION,
    PERSONA_SECTION,
    PromptAssembly,
    PromptContext,
    PromptRegistrationError,
    PromptSection,
    PromptVariableError,
    SystemPrompt,
    TOOL_ORDER_REST,
)

pytestmark = pytest.mark.asyncio


async def mounted(config: dict | None = None, with_tools: bool = False) -> Context:
    root = Context()
    if with_tools:
        await root.plugin(PointsService)
        await root.plugin(ToolsService)
    await root.plugin(SystemPrompt, config or {})
    return root


def tool(name: str, description: str = "", parameters: dict | None = None):
    class _Tool:
        pass

    t = _Tool()
    t.name = name
    t.description = description
    t.parameters = parameters or {}
    t.execute = lambda arguments, execution=None: "ok"
    return t


def section_names(assembly: PromptAssembly) -> list[str]:
    return [s["name"] for s in assembly.sections]


# --------------------------------------------------------------------------- #
# What mounting gives you
# --------------------------------------------------------------------------- #
async def test_identity_and_persona_are_registered_by_default():
    root = await mounted({"persona": "You are Ada."})
    assembly = await root.system_prompt.assemble()
    assert section_names(assembly)[:2] == [HARNESS_IDENTITY_SECTION, PERSONA_SECTION]
    assert "DeepSeek Harness" in root.system_prompt.render_prompt(assembly)
    assert "You are Ada." in root.system_prompt.render_prompt(assembly)


async def test_the_harness_identity_can_be_switched_off():
    root = await mounted({"include_harness_identity": False, "persona": "Ada."})
    assembly = await root.system_prompt.assemble()
    assert HARNESS_IDENTITY_SECTION not in section_names(assembly)


async def test_an_empty_persona_renders_to_nothing():
    """R1.9 — the slot exists at order 0; rendering drops it when it is empty."""
    root = await mounted()
    assembly = await root.system_prompt.assemble()
    assert PERSONA_SECTION in section_names(assembly)
    assert root.system_prompt.render_prompt(assembly).count("\n\n") == 0


# --------------------------------------------------------------------------- #
# Sections (R1)
# --------------------------------------------------------------------------- #
async def test_sections_render_in_order_not_registration_order():
    root = await mounted({"include_harness_identity": False})
    root.system_prompt.section(PromptSection("late", 200, "last"))
    root.system_prompt.section(PromptSection("early", -50, "first"))
    assembly = await root.system_prompt.assemble()
    assert root.system_prompt.render_prompt(assembly) == "first\n\nlast"


async def test_a_tie_in_order_breaks_by_registration():
    """R1.6 — explicit, so an assembly is not dict-order dependent."""
    root = await mounted({"include_harness_identity": False})
    root.system_prompt.section(PromptSection("a", 10, "A"))
    root.system_prompt.section(PromptSection("b", 10, "B"))
    assembly = await root.system_prompt.assemble()
    assert root.system_prompt.render_prompt(assembly) == "A\n\nB"


async def test_a_duplicate_section_name_raises():
    root = await mounted()
    root.system_prompt.section(PromptSection("x", 1, "one"))
    with pytest.raises(PromptRegistrationError, match="already registered"):
        root.system_prompt.section(PromptSection("x", 2, "two"))


async def test_a_non_numeric_order_raises():
    root = await mounted()
    with pytest.raises(TypeError, match="numeric order"):
        root.system_prompt.section(PromptSection("x", "first", "one"))


async def test_a_nan_order_raises():
    root = await mounted()
    with pytest.raises(ValueError, match="NaN"):
        root.system_prompt.section(PromptSection("x", float("nan"), "one"))


async def test_a_callable_section_sees_the_assembly_context():
    """This is what lets a section say something different on step three."""
    root = await mounted({"include_harness_identity": False})
    root.system_prompt.section(
        PromptSection("dyn", 1, lambda ctx: f"step {ctx.get('step')}")
    )
    assembly = await root.system_prompt.assemble({"step": 3})
    assert "step 3" in root.system_prompt.render_prompt(assembly)


async def test_disposing_removes_the_section():
    root = await mounted({"include_harness_identity": False})
    dispose = root.system_prompt.section(PromptSection("x", 1, "gone"))
    assert dispose() is True
    assembly = await root.system_prompt.assemble()
    assert "x" not in section_names(assembly)


async def test_a_stale_disposer_does_not_remove_the_new_registration():
    """Property 1 (I1) — the reference's disposer pops whatever holds the name.

    A fiber unloading after another has taken the name over is normal in a
    plugin system; deleting the live registration is the worst outcome here.
    """
    root = await mounted({"include_harness_identity": False})
    stale = root.system_prompt.section(PromptSection("x", 1, "first"))
    stale()
    root.system_prompt.section(PromptSection("x", 1, "second"))

    assert stale() is False  # the handle knows it no longer owns the name
    assembly = await root.system_prompt.assemble()
    assert "second" in root.system_prompt.render_prompt(assembly)


# --------------------------------------------------------------------------- #
# Complete sections (R1.7, R1.8)
# --------------------------------------------------------------------------- #
async def test_a_complete_section_takes_the_prompt_over():
    root = await mounted({"persona": "Ada."})
    root.system_prompt.section(
        PromptSection("takeover", 500, "ONLY THIS", complete=True)
    )
    assembly = await root.system_prompt.assemble()
    assert section_names(assembly) == ["takeover"]
    assert root.system_prompt.render_prompt(assembly) == "ONLY THIS"


async def test_two_complete_sections_raise_naming_both():
    root = await mounted()
    root.system_prompt.section(PromptSection("a", 1, "A", complete=True))
    root.system_prompt.section(PromptSection("b", 2, "B", complete=True))
    with pytest.raises(PromptRegistrationError) as caught:
        await root.system_prompt.assemble()
    assert "a" in str(caught.value) and "b" in str(caught.value)


# --------------------------------------------------------------------------- #
# Contexts and suppression (R2)
# --------------------------------------------------------------------------- #
async def test_contexts_render_as_a_superseding_snapshot():
    root = await mounted()
    root.system_prompt.context(PromptContext("clock", 1, "It is Tuesday."))
    root.system_prompt.context(PromptContext("cwd", 2, "You are in /tmp."))
    snapshot = root.system_prompt.render_context_snapshot(
        await root.system_prompt.assemble()
    )
    assert snapshot.startswith(CONTEXT_SNAPSHOT_HEADER)
    assert "It is Tuesday.\n\nYou are in /tmp." in snapshot


async def test_contexts_are_not_part_of_the_system_prompt():
    """The distinction the whole context registry exists for."""
    root = await mounted({"include_harness_identity": False})
    root.system_prompt.context(PromptContext("clock", 1, "It is Tuesday."))
    assembly = await root.system_prompt.assemble()
    assert "Tuesday" not in root.system_prompt.render_prompt(assembly)


async def test_an_empty_context_set_renders_to_nothing():
    """R2.5 — not a header with no body."""
    root = await mounted()
    assert root.system_prompt.render_context_snapshot(
        await root.system_prompt.assemble()
    ) == ""


async def test_an_empty_context_is_dropped_from_the_snapshot():
    root = await mounted()
    root.system_prompt.context(PromptContext("quiet", 1, ""))
    root.system_prompt.context(PromptContext("loud", 2, "here"))
    snapshot = root.system_prompt.render_context_snapshot(
        await root.system_prompt.assemble()
    )
    assert snapshot.endswith("here")


async def test_suppression_removes_contexts_from_the_assembly():
    root = await mounted()
    root.system_prompt.context(PromptContext("clock", 1, "It is Tuesday."))
    root.system_prompt.suppress_runtime_context()
    assembly = await root.system_prompt.assemble()
    assert assembly.contexts == []


async def test_suppression_nests(  ):
    """Property 2 (I2) — the reference's release un-suppresses for everyone."""
    root = await mounted()
    root.system_prompt.context(PromptContext("clock", 1, "It is Tuesday."))
    first = root.system_prompt.suppress_runtime_context()
    second = root.system_prompt.suppress_runtime_context()

    first()
    assert root.system_prompt.runtime_context_suppressed is True
    assert (await root.system_prompt.assemble()).contexts == []

    second()
    assert root.system_prompt.runtime_context_suppressed is False
    assert len((await root.system_prompt.assemble()).contexts) == 1


async def test_releasing_twice_does_not_leak_a_suppression():
    root = await mounted()
    release = root.system_prompt.suppress_runtime_context()
    assert release() is True
    assert release() is False
    assert root.system_prompt.runtime_context_suppressed is False


async def test_runtime_context_can_be_off_from_config():
    root = await mounted({"include_runtime_context": False})
    root.system_prompt.context(PromptContext("clock", 1, "It is Tuesday."))
    assert (await root.system_prompt.assemble()).contexts == []


# --------------------------------------------------------------------------- #
# Variables (R3.1)
# --------------------------------------------------------------------------- #
async def test_a_variable_is_resolved_per_assembly():
    root = await mounted({"include_harness_identity": False})
    calls = []

    def who(context):
        calls.append(context.get("turn"))
        return f"turn {context.get('turn')}"

    root.system_prompt.variable("who", who)
    root.system_prompt.section(PromptSection("greet", 1, "hello {{who}}"))

    first = await root.system_prompt.assemble({"turn": 1})
    second = await root.system_prompt.assemble({"turn": 2})
    assert root.system_prompt.render_prompt(first) == "hello turn 1"
    assert root.system_prompt.render_prompt(second) == "hello turn 2"
    assert calls == [1, 2]


async def test_an_illegal_variable_name_is_rejected_at_registration():
    root = await mounted()
    with pytest.raises(ValueError, match="illegal"):
        root.system_prompt.variable("Bad-Name", lambda ctx: "x")


async def test_a_duplicate_variable_raises():
    root = await mounted()
    root.system_prompt.variable("who", lambda ctx: "a")
    with pytest.raises(PromptRegistrationError):
        root.system_prompt.variable("who", lambda ctx: "b")


async def test_an_unresolved_variable_fails_at_render_not_silently():
    root = await mounted()
    root.system_prompt.section(PromptSection("greet", 1, "hello {{nobody}}"))
    assembly = await root.system_prompt.assemble()
    with pytest.raises(PromptVariableError):
        root.system_prompt.render_prompt(assembly)


# --------------------------------------------------------------------------- #
# Tools (R4.1) — the bridge the reference never built
# --------------------------------------------------------------------------- #
async def test_registered_tools_reach_the_assembly():
    """In the reference this list is always empty and `toolOrder` is dead."""
    root = await mounted(with_tools=True)
    root.tools.register(tool("bash", "run a command"))
    assembly = await root.system_prompt.assemble()
    assert [t["name"] for t in assembly.tools] == ["bash"]
    assert assembly.tools[0]["description"] == "run a command"


async def test_the_configured_tool_order_applies_to_registered_tools():
    root = await mounted({"tool_order": ["read", TOOL_ORDER_REST]}, with_tools=True)
    for name in ("write", "bash", "read"):
        root.tools.register(tool(name))
    assembly = await root.system_prompt.assemble()
    assert [t["name"] for t in assembly.tools] == ["read", "bash", "write"]


async def test_a_tool_order_without_the_rest_marker_is_rejected_at_mount():
    root = Context()
    with pytest.raises(Exception, match="rest marker"):
        await root.plugin(SystemPrompt, {"tool_order": ["bash"]})


async def test_a_provider_may_add_schemas_the_registry_does_not_have():
    root = await mounted()
    root.system_prompt.tools(
        lambda ctx: {"schemas": [{"name": "virtual", "description": "", "parameters": {}}]}
    )
    assembly = await root.system_prompt.assemble()
    assert [t["name"] for t in assembly.tools] == ["virtual"]


async def test_the_first_provider_to_name_a_tool_wins():
    root = await mounted()
    root.system_prompt.tools(lambda ctx: {"schemas": [{"name": "x", "description": "first"}]})
    root.system_prompt.tools(lambda ctx: {"schemas": [{"name": "x", "description": "second"}]})
    assembly = await root.system_prompt.assemble()
    assert [t["description"] for t in assembly.tools] == ["first"]


async def test_disposing_a_tool_provider_removes_only_that_registration():
    """The same callable may be registered twice; identity decides, not equality."""
    root = await mounted()

    def provider(ctx):
        return {"schemas": [{"name": "x"}]}

    first = root.system_prompt.tools(provider)
    root.system_prompt.tools(provider)
    assert first() is True
    assert first() is False
    assembly = await root.system_prompt.assemble()
    assert len(assembly.tools) == 1  # deduped by name, but one provider remains


async def test_registered_tools_can_be_left_out():
    root = await mounted({"include_registered_tools": False}, with_tools=True)
    root.tools.register(tool("bash"))
    assert (await root.system_prompt.assemble()).tools == []


# --------------------------------------------------------------------------- #
# The waterfall (R5)
# --------------------------------------------------------------------------- #
async def test_a_listener_can_transform_the_whole_assembly():
    root = await mounted({"include_harness_identity": False})
    root.system_prompt.section(PromptSection("keep", 1, "kept"))

    async def add_one(assembly, context, next_):
        result = await next_()
        return PromptAssembly(
            sections=[*result.sections, {"name": "added", "text": "added by a plugin"}],
            contexts=result.contexts,
            tools=result.tools,
            variables=result.variables,
        )

    root.on(ASSEMBLE_WATERFALL, add_one)
    assembly = await root.system_prompt.assemble()
    assert "added by a plugin" in root.system_prompt.render_prompt(assembly)


async def test_a_listener_cannot_undo_a_complete_section():
    """R5.3 — a complete section is this service's decision, not a default."""
    root = await mounted()
    root.system_prompt.section(PromptSection("only", 1, "ONLY", complete=True))

    async def add_one(assembly, context, next_):
        result = await next_()
        return PromptAssembly(
            sections=[*result.sections, {"name": "sneak", "text": "sneaked in"}],
            contexts=result.contexts,
            tools=result.tools,
            variables=result.variables,
        )

    root.on(ASSEMBLE_WATERFALL, add_one)
    assembly = await root.system_prompt.assemble()
    assert section_names(assembly) == ["only"]


async def test_a_listener_cannot_undo_suppression():
    """R5.4"""
    root = await mounted()
    root.system_prompt.context(PromptContext("clock", 1, "It is Tuesday."))
    root.system_prompt.suppress_runtime_context()

    async def add_context(assembly, context, next_):
        result = await next_()
        return PromptAssembly(
            sections=result.sections,
            contexts=[{"name": "sneak", "text": "sneaked in"}],
            tools=result.tools,
            variables=result.variables,
        )

    root.on(ASSEMBLE_WATERFALL, add_context)
    assert (await root.system_prompt.assemble()).contexts == []


async def test_assembling_twice_gives_the_same_result():
    """Property 3 (I3) — assembly reads the registries, never mutates them."""
    root = await mounted({"persona": "Ada."}, with_tools=True)
    root.tools.register(tool("bash"))
    root.system_prompt.section(PromptSection("x", 1, "text"))
    root.system_prompt.context(PromptContext("c", 1, "ctx"))

    first = await root.system_prompt.assemble()
    second = await root.system_prompt.assemble()
    assert first.sections == second.sections
    assert first.contexts == second.contexts
    assert first.tools == second.tools
