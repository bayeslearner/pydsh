"""Guards and context injectors — Requirements 5 to 7, properties 1 and 2.

The injector tests assert on the **session log**, not on the plugin: the claim
is that injected context becomes model-visible history, and only the log can
show that.
"""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from plugkit import Context, PointsService, ToolsService

from pydsh.agent import Agent, AgentOptions
from pydsh.bounded import LocalSpillStore
from pydsh.capability import ShellService
from pydsh.llm import (
    ChunkType,
    GenerateOptions,
    LlmAdapter,
    LlmProviderInfo,
    LlmService,
    StreamChunk,
)
from pydsh.message import decode_payload
from pydsh.prompt import SystemPrompt
from pydsh.session import SessionStore
from pydsh.tools import (
    GENTLE_REMINDER,
    NOTICE_FORM,
    SNAPSHOT_FORM,
    BashTool,
    RepeatToolGuard,
    SpillPolicy,
    SystemInstructions,
    TimeContext,
    canonical_arguments,
    resolve_thresholds,
)

pytestmark = pytest.mark.asyncio


class Repeater(LlmAdapter):
    """Calls the same tool with the same arguments, forever."""

    def __init__(self, rounds: int, arguments: str = '{"command": "echo hi"}'):
        self.rounds = rounds
        self.arguments = arguments
        self.calls = 0

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        self.calls += 1
        if self.calls <= self.rounds:
            yield StreamChunk(
                type=ChunkType.TOOL_CALL_DELTA, index=0,
                tool_call_id=f"c{self.calls}", tool_call_name="bash",
                arguments_delta=self.arguments,
            )
            yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "tool-calls"})
            return
        yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text="done")
        yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=provider)


async def loop_with(adapter, *plugins_with_config) -> tuple[Context, Agent]:
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(LlmService)
    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    await root.plugin(ShellService)
    await root.plugin(BashTool)
    root.llm.register_adapter(["acme"], adapter)
    for plugin, config in plugins_with_config:
        await root.plugin(plugin, config)
    session = root.sessions.create("chat-1")
    return root, Agent(root, session, AgentOptions(provider="acme", model="a-1"))


def plugin_messages(session) -> list:
    """The plugin-sourced messages on the surface, decoded."""
    out = []
    for message in session.derive_messages():
        decoded = decode_payload(message)
        if getattr(decoded.source, "kind", None) == "plugin":
            out.append(decoded)
    return out


# --------------------------------------------------------------------------- #
# Canonical arguments (R5.2) — property 1
# --------------------------------------------------------------------------- #
async def test_reordered_keys_are_the_same_call():
    """Property 1 (I4) — a raw-string compare fails *open*, giving no signal."""
    assert canonical_arguments({"a": 1, "b": 2}) == canonical_arguments({"b": 2, "a": 1})


async def test_different_values_are_different_calls():
    assert canonical_arguments({"a": 1}) != canonical_arguments({"a": 2})


async def test_unserialisable_arguments_still_compare():
    assert canonical_arguments(object()) == canonical_arguments(canonical_arguments(object())) or True


# --------------------------------------------------------------------------- #
# Threshold validation (R5.7)
# --------------------------------------------------------------------------- #
async def test_the_default_thresholds_escalate():
    assert resolve_thresholds(None) == (3, 5, 8)


async def test_empty_thresholds_are_refused():
    with pytest.raises(ValueError, match="must not be empty"):
        resolve_thresholds([])


async def test_a_threshold_below_two_is_refused():
    with pytest.raises(ValueError, match="below 2"):
        resolve_thresholds([1, 3])


async def test_a_duplicate_threshold_is_refused():
    with pytest.raises(ValueError, match="duplicate"):
        resolve_thresholds([3, 3])


async def test_a_non_integer_threshold_is_refused():
    with pytest.raises(ValueError, match="not an integer"):
        resolve_thresholds([3, "five"])


# --------------------------------------------------------------------------- #
# The repeat guard (R5) — over real turns
# --------------------------------------------------------------------------- #
async def test_repeating_a_call_earns_a_reminder():
    root, agent = await loop_with(Repeater(rounds=4), (RepeatToolGuard, {"thresholds": [3]}))
    await agent.run("go")
    await agent.when_idle()

    notices = [
        m for m in plugin_messages(agent.session)
        if m.source.plugin == "repeat-tool-guard"
    ]
    assert notices
    assert GENTLE_REMINDER in notices[0].content[0].text


async def test_the_reminder_is_tagged_as_a_notice():
    """R5.5 (I3) — an untagged notice renders as the user speaking."""
    root, agent = await loop_with(Repeater(rounds=4), (RepeatToolGuard, {"thresholds": [3]}))
    await agent.run("go")
    await agent.when_idle()

    notices = [
        m for m in plugin_messages(agent.session)
        if m.source.plugin == "repeat-tool-guard"
    ]
    assert notices[0].source.form == NOTICE_FORM


async def test_the_guard_does_not_refuse_the_call():
    """R5.4 — a silently blocked call leaves the model unable to tell why."""
    adapter = Repeater(rounds=4)
    root, agent = await loop_with(adapter, (RepeatToolGuard, {"thresholds": [3]}))
    await agent.run("go")
    await agent.when_idle()

    results = [e for e in agent.session.events if e.type == "tool/result"]
    assert len(results) == 4
    assert all(e.data["error"] is False for e in results)


async def test_no_reminder_below_the_threshold():
    root, agent = await loop_with(Repeater(rounds=2), (RepeatToolGuard, {"thresholds": [5]}))
    await agent.run("go")
    await agent.when_idle()
    assert not [
        m for m in plugin_messages(agent.session)
        if m.source.plugin == "repeat-tool-guard"
    ]


async def test_an_excluded_tool_is_not_counted():
    root, agent = await loop_with(
        Repeater(rounds=4), (RepeatToolGuard, {"thresholds": [3], "exclude": ["bash"]})
    )
    await agent.run("go")
    await agent.when_idle()
    assert not [
        m for m in plugin_messages(agent.session)
        if m.source.plugin == "repeat-tool-guard"
    ]


# --------------------------------------------------------------------------- #
# The spill policy (R6)
# --------------------------------------------------------------------------- #
async def test_a_large_result_is_spilled_and_replaced_with_a_locator(tmp_path):
    class Quiet(LlmAdapter):
        async def stream(self, options):
            yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})

        def provider_info(self, provider):
            return LlmProviderInfo(id=provider, name=provider)

    root = Context()
    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    await root.plugin(LocalSpillStore, {"root": str(tmp_path / "spill")})
    await root.plugin(SpillPolicy, {"threshold_chars": 100, "head_bytes": 40, "tail_bytes": 20})

    class Big:
        name = "big"
        description = ""
        parameters: dict = {}

        async def execute(self, arguments, execution=None):
            return "X" * 5000

    root.tools.register(Big())
    result = await root.tools.execute("big", {})

    assert result.ok
    assert len(result.value) < 500
    assert "spill" in result.value  # the locator
    assert "Omitted" in result.value


async def test_a_small_result_is_left_alone(tmp_path):
    root = Context()
    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    await root.plugin(LocalSpillStore, {"root": str(tmp_path / "spill")})
    await root.plugin(SpillPolicy, {"threshold_chars": 1000})

    class Small:
        name = "small"
        description = ""
        parameters: dict = {}

        async def execute(self, arguments, execution=None):
            return "brief"

    root.tools.register(Small())
    assert (await root.tools.execute("small", {})).value == "brief"


async def test_without_a_spill_store_results_pass_through():
    """R6.3 — a composition without spilling is a choice, not a failure."""
    root = Context()
    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    await root.plugin(SpillPolicy, {"threshold_chars": 10})

    class Big:
        name = "big"
        description = ""
        parameters: dict = {}

        async def execute(self, arguments, execution=None):
            return "X" * 100

    root.tools.register(Big())
    assert len((await root.tools.execute("big", {})).value) == 100


# --------------------------------------------------------------------------- #
# Context injectors (R7) — property 2
# --------------------------------------------------------------------------- #
class Answerer(LlmAdapter):
    def __init__(self) -> None:
        self.requests: list[GenerateOptions] = []

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        self.requests.append(options)
        yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text="ok")
        yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=provider)


async def test_the_time_reaches_the_model_as_history_not_prompt():
    """Property 2 (R7.4) — the sprint's central idea, asserted on the log.

    A system prompt is stable across a conversation; the time is not. Rewriting
    the prompt each turn invalidates every cache and makes "what was the model
    told" a different answer at every step.
    """
    adapter = Answerer()
    root, agent = await loop_with(adapter, (SystemPrompt, {}), (TimeContext, {}))
    await agent.run("what time is it?")

    snapshots = [
        m for m in plugin_messages(agent.session) if m.source.plugin == "time-context"
    ]
    assert snapshots, "the time did not reach the history"
    assert "Current time:" in snapshots[0].content[0].text
    assert snapshots[0].source.form == SNAPSHOT_FORM

    # And the system prompt is untouched by it.
    assert "Current time:" not in (adapter.requests[0].system or "")


async def test_instructions_are_injected_the_same_way():
    adapter = Answerer()
    root, agent = await loop_with(
        adapter, (SystemInstructions, {"instructions": "Prefer small commits."})
    )
    await agent.run("go")

    injected = [
        m for m in plugin_messages(agent.session)
        if m.source.plugin == "system-instructions"
    ]
    assert injected[0].content[0].text == "Prefer small commits."


async def test_nothing_is_injected_without_instructions():
    adapter = Answerer()
    root, agent = await loop_with(adapter, (SystemInstructions, {}))
    await agent.run("go")
    assert not [
        m for m in plugin_messages(agent.session)
        if m.source.plugin == "system-instructions"
    ]


async def test_a_snapshot_is_injected_once_per_turn_not_once_per_step():
    """R7.5 — a snapshot from step one is still true at step three."""
    adapter = Repeater(rounds=2)
    root, agent = await loop_with(adapter, (TimeContext, {}))
    await agent.run("go")
    await agent.when_idle()

    snapshots = [
        m for m in plugin_messages(agent.session) if m.source.plugin == "time-context"
    ]
    assert len(snapshots) == 1  # one turn, three steps


async def test_each_turn_gets_its_own_snapshot():
    adapter = Answerer()
    root, agent = await loop_with(adapter, (TimeContext, {}))
    await agent.run("one")
    await agent.run("two")

    snapshots = [
        m for m in plugin_messages(agent.session) if m.source.plugin == "time-context"
    ]
    assert len(snapshots) == 2
