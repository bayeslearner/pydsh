"""Plan mode — Requirement 1, property 1.

The tests that matter are about *when* a flip takes effect. A turn must run
under one set of rules from its first step to its last, so the interesting
cases are all the ones where someone changes their mind halfway.
"""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from plugkit import Context, PointsService, ToolsService

from pydsh import (
    Agent,
    AgentOptions,
    ChunkType,
    Commands,
    GenerateOptions,
    LlmAdapter,
    LlmProviderInfo,
    LlmService,
    PlanMode,
    PlanModeError,
    SessionProjections,
    SessionStore,
    StreamChunk,
    SystemPrompt,
    decode_payload,
    fold_plan_mode,
)
from pydsh.plan import (
    EXIT_PLAN_MODE,
    PLAN_EVENT,
    PLAN_KEY,
    POLICY_SECTION,
    has_open_turn,
)

pytestmark = pytest.mark.asyncio

GUIDANCE = "While planning: do not edit files. Present a plan first."


class Answerer(LlmAdapter):
    def __init__(self, reply: str = "ok") -> None:
        self.reply = reply
        self.requests: list[GenerateOptions] = []

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        self.requests.append(options)
        yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text=self.reply)
        yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=provider)


class TwoStepper(LlmAdapter):
    """Calls a tool once, then answers — so a turn has two steps."""

    def __init__(self, tool: str = "noop") -> None:
        self.tool = tool
        self.calls = 0
        self.requests: list[GenerateOptions] = []

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        self.requests.append(options)
        self.calls += 1
        if self.calls == 1:
            yield StreamChunk(
                type=ChunkType.TOOL_CALL_DELTA, index=0,
                tool_call_id="c1", tool_call_name=self.tool,
                arguments_delta="{}",
            )
            yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "tool-calls"})
            return
        yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text="done")
        yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})


class _Noop:
    name = "noop"
    description = "does nothing"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, arguments, execution=None) -> str:
        return "nothing happened"


async def build(adapter=None, *, prompt=True, projections=True, commands=True):
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(LlmService)
    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    if projections:
        await root.plugin(SessionProjections)
    if commands:
        await root.plugin(Commands)
    if prompt:
        await root.plugin(SystemPrompt)
    await root.plugin(PlanMode, {"section": GUIDANCE})
    adapter = adapter or Answerer()
    root.llm.register_adapter(["acme"], adapter)
    session = root.sessions.create("chat-1")
    agent = Agent(root, session, AgentOptions(provider="acme", model="a-1"))
    return root, agent, adapter


def plan_events(session) -> list:
    return [e for e in session.events if e.type == PLAN_EVENT]


# --------------------------------------------------------------------------- #
# R1.1 — configuration
# --------------------------------------------------------------------------- #
async def test_a_missing_section_fails_at_construction():
    """A deployment with no guidance must not reach a running plan mode."""
    root = Context()
    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    with pytest.raises(PlanModeError):
        await root.plugin(PlanMode, {})


async def test_a_blank_section_fails_at_construction():
    root = Context()
    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    with pytest.raises(PlanModeError):
        await root.plugin(PlanMode, {"section": "   "})


async def test_an_unknown_config_key_fails():
    root = Context()
    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    with pytest.raises(PlanModeError):
        await root.plugin(PlanMode, {"section": "x", "sectoin": "typo"})


# --------------------------------------------------------------------------- #
# R1.2, R1.3 — the fold and what set() reports
# --------------------------------------------------------------------------- #
async def test_the_last_event_wins():
    """R1.2, I1."""
    root, agent, _ = await build()
    session = agent.session
    session.append(PLAN_EVENT, {"active": True})
    session.append(PLAN_EVENT, {"active": False})
    session.append(PLAN_EVENT, {"active": True})
    assert fold_plan_mode(session.events) is True


async def test_setting_while_idle_commits():
    root, agent, _ = await build()
    assert root.plan_mode.set(agent, True) == "committed"
    assert root.plan_mode.get(agent) == {"active": True}
    assert len(plan_events(agent.session)) == 1


async def test_setting_what_is_already_true_is_a_noop():
    root, agent, _ = await build()
    root.plan_mode.set(agent, True)
    assert root.plan_mode.set(agent, True) == "noop"
    assert len(plan_events(agent.session)) == 1


# --------------------------------------------------------------------------- #
# R1.4, R1.5 — the boundary (I2)
# --------------------------------------------------------------------------- #
async def test_a_flip_during_an_open_turn_is_queued():
    root, agent, _ = await build()
    session = agent.session
    session.append("turn/start", {"turn": 1})

    assert root.plan_mode.set(agent, True) == "queued"
    assert plan_events(session) == []  # nothing recorded yet
    assert root.plan_mode.get(agent) == {"active": False, "pending": True}


async def test_a_queued_flip_that_restores_the_recorded_state_cancels():
    """R1.5 — and does not leave a pending write that would do nothing."""
    root, agent, _ = await build()
    session = agent.session
    session.append("turn/start", {"turn": 1})

    assert root.plan_mode.set(agent, True) == "queued"
    assert root.plan_mode.set(agent, False) == "cancelled"
    assert "pending" not in root.plan_mode.get(agent)
    assert plan_events(session) == []


async def test_a_queued_flip_lands_at_the_next_turn():
    root, agent, adapter = await build()
    session = agent.session
    session.append("turn/start", {"turn": 1})
    root.plan_mode.set(agent, True)
    session.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})

    await agent.run("carry on")
    await agent.when_idle()

    assert fold_plan_mode(session.events) is True
    assert len(plan_events(session)) == 1


async def test_a_turn_runs_under_one_set_of_rules():
    """Property 1 (R1.4, I2) — the reason pending exists at all.

    The reference prefers the *pending* value when rendering the policy
    section, so a flip requested mid-turn reaches step two of the turn it was
    queued out of — applied and queued at the same time.
    """
    adapter = TwoStepper()
    root, agent, _ = await build(adapter)
    root.tools.register(_Noop())

    # Enter plan mode from inside the turn, at the first tool call.
    async def flip(payload, next_):
        decision = await next_()
        if payload.get("step") == 1:
            root.plan_mode.set(payload["agent"], True)
        return decision

    root.on("agent/pre-step", flip)
    await agent.run("go")
    await agent.when_idle()

    assert adapter.calls == 2
    systems = [r.system or "" for r in adapter.requests]
    assert all(GUIDANCE not in s for s in systems), (
        "the policy reached a turn that began without it"
    )


# --------------------------------------------------------------------------- #
# R1.6 — the prompt section
# --------------------------------------------------------------------------- #
async def test_the_guidance_reaches_the_model_while_plan_mode_is_on():
    root, agent, adapter = await build()
    root.plan_mode.set(agent, True)
    await agent.run("what should we do?")
    await agent.when_idle()
    assert GUIDANCE in (adapter.requests[0].system or "")


async def test_the_section_is_empty_while_plan_mode_is_off():
    root, agent, adapter = await build()
    await agent.run("what should we do?")
    await agent.when_idle()
    assert GUIDANCE not in (adapter.requests[0].system or "")


async def test_the_section_renders_empty_without_an_agent():
    """Assembly with no agent in context must not raise, or crash a prompt."""
    root, agent, _ = await build()
    assembly = await root.system_prompt.assemble({})
    rendered = root.system_prompt.render_prompt(assembly)
    assert GUIDANCE not in rendered


# --------------------------------------------------------------------------- #
# R1.7 — /plan
# --------------------------------------------------------------------------- #
async def test_plan_on_and_off():
    root, agent, _ = await build()
    on = await root.commands.invoke("plan", agent=agent)
    assert on.kind == "success" and fold_plan_mode(agent.session.events)

    off = await root.commands.invoke("plan", agent=agent, raw_input="off")
    assert off.kind == "success" and not fold_plan_mode(agent.session.events)


async def test_plan_with_text_delivers_it_as_a_message():
    root, agent, adapter = await build()
    await root.commands.invoke("plan", agent=agent, raw_input="rewrite the parser")
    await agent.when_idle()

    texts = [
        decode_payload(m).content[0].text
        for m in agent.session.derive_messages()
        if decode_payload(m).role == "user"
    ]
    assert "rewrite the parser" in texts


async def test_plan_off_when_already_off():
    root, agent, _ = await build()
    result = await root.commands.invoke("plan", agent=agent, raw_input="off")
    assert result.kind == "success" and "already inactive" in result.text


# --------------------------------------------------------------------------- #
# R1.8 — exit_plan_mode
# --------------------------------------------------------------------------- #
async def _exit(root, agent, plan: str):
    return await root.tools.execute(EXIT_PLAN_MODE, {"plan": plan}, caller=agent)


async def test_exit_plan_mode_refuses_outside_plan_mode():
    root, agent, _ = await build()
    result = await _exit(root, agent, "# A plan\n\nDo the thing.")
    assert "only available in plan mode" in str(result.value)


async def test_exit_plan_mode_refuses_a_plan_that_is_not_markdown():
    root, agent, _ = await build()
    root.plan_mode.set(agent, True)
    for bad in ("", "just some prose", "#", "## not a top heading"):
        result = await _exit(root, agent, bad)
        assert "markdown" in str(result.value)


async def test_exit_plan_mode_refuses_when_no_review_channel_is_mounted():
    """R1.8 — an approval nobody gave is the one thing it must never invent."""
    root, agent, _ = await build()
    root.plan_mode.set(agent, True)
    result = await _exit(root, agent, "# A plan\n\nDo the thing.")
    text = str(result.value)
    assert "ctx.user_questions" in text and "approved" not in text


async def test_exit_plan_mode_queues_the_exit_once_approved():
    class Channel:
        async def ask_approval(self, agent, plan):
            self.plan = plan
            return True

    root, agent, _ = await build()
    root.user_questions = Channel()
    root.plan_mode.set(agent, True)

    result = await _exit(root, agent, "# A plan\n\nDo the thing.")
    assert '"approved": true' in str(result.value)
    assert root.plan_mode.get(agent)["pending"] is False


# --------------------------------------------------------------------------- #
# R1.9 — the projection
# --------------------------------------------------------------------------- #
async def test_the_projection_reports_the_recorded_state():
    root, agent, _ = await build()
    snapshot = root.session_projections.snapshot(agent.session)["values"]
    assert snapshot[PLAN_KEY] == {"active": False}

    root.plan_mode.set(agent, True)
    assert root.session_projections.snapshot(agent.session)["values"][PLAN_KEY] == {
        "active": True
    }


async def test_the_projection_does_not_claim_a_pending_intent():
    """It folds the log, and a pending intent is deliberately not in the log."""
    root, agent, _ = await build()
    agent.session.append("turn/start", {"turn": 1})
    root.plan_mode.set(agent, True)
    assert root.session_projections.snapshot(agent.session)["values"][PLAN_KEY] == {
        "active": False
    }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
async def test_has_open_turn():
    root, agent, _ = await build()
    session = agent.session
    assert not has_open_turn(session.events)
    session.append("turn/start", {"turn": 1})
    assert has_open_turn(session.events)
    session.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
    assert not has_open_turn(session.events)


async def test_plan_mode_works_without_prompt_projections_or_commands():
    """Every one of them is optional; none of them may be assumed."""
    root, agent, _ = await build(prompt=False, projections=False, commands=False)
    assert root.plan_mode.set(agent, True) == "committed"
