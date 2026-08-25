"""The loop using the prompt registry — Requirement 6.

Every assertion here is on what the *adapter actually received*, because that
is the only thing that proves a plugin's registered section reached the model.
Asserting on the assembly would prove the service works, which is a different
claim and already covered.
"""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from plugkit import Context, PointsService, ToolsService

from pydsh.agent import Agent, AgentOptions
from pydsh.llm import (
    ChunkType,
    GenerateOptions,
    LlmAdapter,
    LlmProviderInfo,
    LlmService,
    StreamChunk,
)
from pydsh.prompt import PromptSection, SystemPrompt, TOOL_ORDER_REST
from pydsh.session import SessionStore

pytestmark = pytest.mark.asyncio


class Recorder(LlmAdapter):
    """Answers once, and keeps every request it was given."""

    def __init__(self) -> None:
        self.requests: list[GenerateOptions] = []

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        self.requests.append(options)
        yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text="ok")
        yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=provider)


def tool(name: str):
    class _Tool:
        pass

    t = _Tool()
    t.name = name
    t.description = f"the {name} tool"
    t.parameters = {}
    t.execute = lambda arguments, execution=None: "ok"
    return t


async def build(
    prompt_config: dict | None = None,
    with_prompt: bool = True,
    with_tools: bool = False,
    **agent_options,
) -> tuple[Context, Agent, Recorder]:
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(LlmService)
    if with_tools:
        await root.plugin(PointsService)
        await root.plugin(ToolsService)
    if with_prompt:
        await root.plugin(SystemPrompt, prompt_config or {})
    adapter = Recorder()
    root.llm.register_adapter(["acme"], adapter)
    session = root.sessions.create()
    options = AgentOptions(provider="acme", model="a-1", **agent_options)
    return root, Agent(root, session, options), adapter


# --------------------------------------------------------------------------- #
# R6.1, R6.2 — which source the loop uses
# --------------------------------------------------------------------------- #
async def test_without_the_service_the_loop_uses_agent_options():
    """R6.2 — sprint 03's behaviour is the fallback, and still works."""
    root, agent, adapter = await build(with_prompt=False, system="from options")
    await agent.run("go")
    assert adapter.requests[0].system == "from options"


async def test_with_the_service_the_registry_wins():
    """R6.1 — a plugin's section reaches the model; AgentOptions.system does not."""
    root, agent, adapter = await build(
        {"include_harness_identity": False, "persona": "You are Ada."},
        system="ignored once the service is mounted",
    )
    await agent.run("go")
    assert adapter.requests[0].system == "You are Ada."


async def test_a_plugin_section_reaches_the_model():
    root, agent, adapter = await build({"include_harness_identity": False})
    root.system_prompt.section(PromptSection("fs", 100, "Prefer relative paths."))
    await agent.run("go")
    assert "Prefer relative paths." in adapter.requests[0].system


async def test_an_empty_assembly_sends_no_system_prompt():
    """R6.5 — a present-but-blank system message is not the same as none."""
    root, agent, adapter = await build({"include_harness_identity": False})
    await agent.run("go")
    assert adapter.requests[0].system is None


# --------------------------------------------------------------------------- #
# R6.3 — the assembly sees the live run
# --------------------------------------------------------------------------- #
async def test_the_assembly_context_carries_the_live_run():
    seen: list[dict] = []
    root, agent, adapter = await build({"include_harness_identity": False})
    root.system_prompt.section(
        PromptSection("probe", 1, lambda ctx: (seen.append(ctx), "probed")[1])
    )
    await agent.run("go")

    context = seen[0]
    assert context["agent"] is agent
    assert context["session"] is agent.session
    assert context["turn"] == 1
    assert context["step"] == 1
    assert context["signal"] is not None


async def test_a_section_can_differ_per_step():
    """The reason section text may be a callable at all."""
    root, agent, adapter = await build({"include_harness_identity": False})
    root.system_prompt.section(
        PromptSection("turn", 1, lambda ctx: f"this is turn {ctx['turn']}")
    )
    await agent.run("one")
    await agent.run("two")
    assert adapter.requests[0].system == "this is turn 1"
    assert adapter.requests[1].system == "this is turn 2"


# --------------------------------------------------------------------------- #
# R6.4 — tools come through the assembly
# --------------------------------------------------------------------------- #
async def test_registered_tools_still_reach_the_model_through_the_assembly():
    """Mounting the prompt service must not silently remove the agent's tools."""
    root, agent, adapter = await build(with_tools=True)
    root.tools.register(tool("bash"))
    await agent.run("go")
    assert [t["name"] for t in adapter.requests[0].tools] == ["bash"]


async def test_the_configured_tool_order_reaches_the_model():
    """The whole point of `tool_order` — dead in the reference, live here."""
    root, agent, adapter = await build(
        {"tool_order": ["read", TOOL_ORDER_REST]}, with_tools=True
    )
    for name in ("write", "bash", "read"):
        root.tools.register(tool(name))
    await agent.run("go")
    assert [t["name"] for t in adapter.requests[0].tools] == ["read", "bash", "write"]


async def test_no_tools_still_sends_no_tool_list():
    root, agent, adapter = await build()
    await agent.run("go")
    assert adapter.requests[0].tools is None
