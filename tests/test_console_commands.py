"""The console commands — Requirements 3–5, property 3.

`/compact` runs against the real compaction engine over a real session, and
`/goal` against the real goal service. The rule every test here is checking, in
one form or another, is that a person who typed something gets a sentence back
— never a traceback, never a `GoalError`, never an unhandled refusal.
"""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from plugkit import Context, PointsService, Service, ToolsService

from pydsh import (
    Agent,
    AgentOptions,
    AnonymousUserId,
    BasicCompaction,
    ChunkType,
    Commands,
    CompactCommand,
    CompactionRefused,
    FeedbackCommand,
    GenerateOptions,
    GoalCommand,
    GoalService,
    LlmAdapter,
    LlmProviderInfo,
    LlmService,
    SessionStore,
    StreamChunk,
    TokenMeter,
    record_feedback,
)
from pydsh.console import CANCELLED, REFUSALS, parse_goal_command
from pydsh.console.feedback import (
    FEEDBACK_EVENT,
    UNCONFIGURED,
    SessionFeedbackError,
    sharing_disclosure,
)

pytestmark = pytest.mark.asyncio


class Answerer(LlmAdapter):
    def __init__(self, reply: str = "ok") -> None:
        self.reply = reply

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text=self.reply)
        yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=provider)


class StubCompaction(Service):
    """Stands in for the engine so each outcome can be produced on demand.

    A real plugkit service, not a plain attribute: `inject` is the permission
    list as well as the requirement list, so a command injecting `compaction`
    cannot see an object simply assigned onto the context.
    """

    provide = "compaction"

    def __init__(self, ctx, config=None) -> None:
        super().__init__(ctx)
        self.outcome = (config or {}).get("outcome")
        self.calls = 0

    async def compact_now(self, agent, signal=None, source_command_id=None):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


async def base(*, goals=False, feedback=False, identity=False):
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(LlmService)
    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    await root.plugin(Commands)
    if goals:
        await root.plugin(GoalService)
        await root.plugin(GoalCommand)
    if identity:
        await root.plugin(AnonymousUserId)
    if feedback:
        await root.plugin(FeedbackCommand)
    root.llm.register_adapter(["acme"], Answerer())
    session = root.sessions.create("chat-1")
    agent = Agent(root, session, AgentOptions(provider="acme", model="a-1"))
    return root, agent


async def with_compaction(outcome):
    root, agent = await base()
    await root.plugin(StubCompaction, {"outcome": outcome})
    await root.plugin(CompactCommand)
    return root, agent


# --------------------------------------------------------------------------- #
# R3 — /compact
# --------------------------------------------------------------------------- #
async def test_compact_reports_what_it_shadowed():
    class Result:
        shadowed_seqs = [3, 4, 5]
        shadowed_tokens = 812
        summary_seq = 9

    root, agent = await with_compaction(Result())
    result = await root.commands.invoke("compact", agent=agent)
    assert result.kind == "success"
    assert "3 entries" in result.text and "812" in result.text
    assert result.source_event_seq == 9


async def test_nothing_to_compact_is_a_success():
    """R3.3 — an ordinary outcome, not a failure."""
    root, agent = await with_compaction(None)
    result = await root.commands.invoke("compact", agent=agent)
    assert result.kind == "success" and "no history to compact" in result.text


async def test_compact_takes_no_arguments():
    root, agent = await with_compaction(None)
    result = await root.commands.invoke("compact", agent=agent, raw_input="everything")
    assert result.kind == "error" and "no arguments" in result.text
    assert root.compaction.calls == 0


async def test_each_refusal_becomes_its_own_sentence():
    """R3.4 — routed by code, because the message names sequence numbers."""
    for code in ("empty", "unbalanced", "changed", "commit"):
        root, agent = await with_compaction(
            CompactionRefused("surface node 41 would split a pair", code)
        )
        result = await root.commands.invoke("compact", agent=agent)
        assert result.kind == "error"
        assert result.text == REFUSALS[code]
        assert "41" not in result.text


async def test_an_unknown_refusal_code_still_says_something_true():
    root, agent = await with_compaction(CompactionRefused("who knows", "martian"))
    result = await root.commands.invoke("compact", agent=agent)
    assert result.text == REFUSALS["refused"]


async def test_a_cancelled_compaction_says_cancelled():
    """R3.5 — not the refusal the cancellation caused."""

    class Aborted:
        aborted = True

    root, agent = await with_compaction(CompactionRefused("changed under us", "changed"))
    result = await root.commands.invoke("compact", agent=agent, signal=Aborted())
    assert result.text == CANCELLED


async def test_compact_over_the_real_engine():
    """Not a stub: the command against `BasicCompaction` and a real session."""
    root, agent = await base()
    await root.plugin(TokenMeter)
    await root.plugin(BasicCompaction, {"keep_recent_messages": 0})
    await root.plugin(CompactCommand)

    result = await root.commands.invoke("compact", agent=agent)
    # An empty conversation has nothing balanced to compact — which is exactly
    # the ordinary success R3.3 describes, and it must not raise.
    assert result.kind == "success"


# --------------------------------------------------------------------------- #
# R4 — /goal
# --------------------------------------------------------------------------- #
async def test_parse_goal_command():
    """R4.1 — the reserved words, and everything else as an objective."""
    assert parse_goal_command("")["kind"] == "show"
    assert parse_goal_command("  ")["kind"] == "show"
    assert parse_goal_command("clear")["kind"] == "clear"
    assert parse_goal_command("PAUSE")["kind"] == "pause"
    assert parse_goal_command("resume")["kind"] == "resume"
    assert parse_goal_command("edit")["kind"] == "invalid-edit"
    assert parse_goal_command("edit   ")["kind"] == "invalid-edit"
    assert parse_goal_command("edit ship it") == {
        "kind": "edit",
        "objective": "ship it",
    }
    assert parse_goal_command("edited the parser")["kind"] == "create"
    assert parse_goal_command("clear the build cache")["kind"] == "create"


async def test_show_with_no_goal():
    root, agent = await base(goals=True)
    result = await root.commands.invoke("goal", agent=agent)
    assert result.kind == "success" and "No goal is set" in result.text


async def test_set_then_show():
    root, agent = await base(goals=True)
    created = await root.commands.invoke("goal", agent=agent, raw_input="ship the port")
    assert created.kind == "success" and "ship the port" in created.text

    shown = await root.commands.invoke("goal", agent=agent)
    assert "Status: active" in shown.text
    assert "Objective: ship the port" in shown.text
    assert "Rounds: 0/" in shown.text
    assert "/goal pause" in shown.text


async def test_creating_over_an_active_goal_is_refused_with_the_alternatives():
    """R4.4."""
    root, agent = await base(goals=True)
    await root.commands.invoke("goal", agent=agent, raw_input="first")
    result = await root.commands.invoke("goal", agent=agent, raw_input="second")
    assert result.kind == "error"
    assert "/goal edit" in result.text and "/goal clear" in result.text


async def test_edit_pause_resume_and_clear():
    root, agent = await base(goals=True)
    await root.commands.invoke("goal", agent=agent, raw_input="first")

    edited = await root.commands.invoke("goal", agent=agent, raw_input="edit second")
    assert "Objective: second" in edited.text

    paused = await root.commands.invoke("goal", agent=agent, raw_input="pause")
    assert "Status: paused" in paused.text
    assert "/goal resume" in paused.text

    resumed = await root.commands.invoke("goal", agent=agent, raw_input="resume")
    assert "Status: active" in resumed.text

    cleared = await root.commands.invoke("goal", agent=agent, raw_input="clear")
    assert cleared.kind == "success" and "cleared" in cleared.text
    assert root.goals.current(agent.session) is None


async def test_edit_with_no_objective_names_the_usage():
    """R4.2."""
    root, agent = await base(goals=True)
    result = await root.commands.invoke("goal", agent=agent, raw_input="edit")
    assert result.kind == "error" and "/goal [" in result.text


async def test_pause_with_no_goal():
    root, agent = await base(goals=True)
    result = await root.commands.invoke("goal", agent=agent, raw_input="pause")
    assert result.kind == "error" and "No goal is set" in result.text


async def test_clear_with_no_goal_is_a_success():
    root, agent = await base(goals=True)
    result = await root.commands.invoke("goal", agent=agent, raw_input="clear")
    assert result.kind == "success" and "no goal to clear" in result.text


async def test_an_unreadable_goal_log_comes_back_readable():
    """R4.5, property 3 — a GoalError never reaches the person.

    Reached through a *read*: a `goal/change` this version cannot decode makes
    the fold itself raise, which is what a log written by a newer build looks
    like from here.
    """
    root, agent = await base(goals=True)
    agent.session.append("goal/change", {"version": 99, "operation": "wat"})

    result = await root.commands.invoke("goal", agent=agent)
    assert result.kind == "error"
    assert "Run /goal" in result.text


async def test_never_shows_a_revision_number():
    """R4.3 — compare-and-set is the service's business, not the person's."""
    root, agent = await base(goals=True)
    created = await root.commands.invoke("goal", agent=agent, raw_input="ship it")
    assert "revision" not in created.text.lower()


# --------------------------------------------------------------------------- #
# R5 — /feedback
# --------------------------------------------------------------------------- #
async def test_feedback_is_recorded_log_only():
    """R5.1 — on the log, never on the surface."""
    root, agent = await base(feedback=True, identity=True)
    result = await root.commands.invoke(
        "feedback", agent=agent, raw_input="the tool loop is too chatty"
    )
    assert result.kind == "success"

    recorded = [e for e in agent.session.events if e.type == FEEDBACK_EVENT]
    assert recorded and recorded[0].data["text"] == "the tool loop is too chatty"
    assert agent.session.derive_messages() == []


async def test_empty_feedback_leaves_no_event():
    """R5.2."""
    root, agent = await base(feedback=True)
    result = await root.commands.invoke("feedback", agent=agent, raw_input="   ")
    assert result.kind == "error" and "required" in result.text
    assert [e for e in agent.session.events if e.type == FEEDBACK_EVENT] == []


async def test_record_feedback_refuses_empty_text_directly():
    root, agent = await base()
    with pytest.raises(SessionFeedbackError):
        record_feedback(agent.session, "  ")
    assert agent.session.events == ()


async def test_the_reply_names_the_session_and_the_user():
    """R5.3."""
    root, agent = await base(feedback=True, identity=True)
    result = await root.commands.invoke("feedback", agent=agent, raw_input="good")
    assert "chat-1" in result.text
    assert root.anonymous_user_id.value in result.text


async def test_unconfigured_sharing_is_disclosed_as_such():
    """R5.4 — and the command still works without telemetry."""
    root, agent = await base(feedback=True)
    result = await root.commands.invoke("feedback", agent=agent, raw_input="fine")
    assert UNCONFIGURED in result.text


async def test_each_sharing_policy_has_its_own_disclosure():
    class Telemetry:
        def __init__(self, sharing):
            self.sharing = sharing

    assert "enabled" in sharing_disclosure(Telemetry("full"))
    assert "feedback-gated" in sharing_disclosure(Telemetry("feedback-only"))
    assert "disabled" in sharing_disclosure(Telemetry("disabled"))
    assert "disabled" in sharing_disclosure(Telemetry("something-new"))
    assert sharing_disclosure(None) == UNCONFIGURED


# --------------------------------------------------------------------------- #
# Property 3 — nothing raises at the person
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "command,raw",
    [
        ("goal", ""),
        ("goal", "edit"),
        ("goal", "pause"),
        ("goal", "resume"),
        ("goal", "clear"),
        ("goal", "a brand new objective"),
        ("feedback", ""),
        ("feedback", "something"),
    ],
)
async def test_no_input_produces_an_exception(command, raw):
    """Property 3 (I5) — every path ends in a CommandResult."""
    root, agent = await base(goals=True, feedback=True)
    result = await root.commands.invoke(command, agent=agent, raw_input=raw)
    assert result.kind in ("success", "error")
    assert isinstance(result.text, str) and result.text


async def test_a_command_without_a_session_says_so_rather_than_crashing():
    root, agent = await base(goals=True, feedback=True)
    for command in ("goal", "feedback"):
        result = await root.commands.invoke(command, agent=None, raw_input="x")
        assert result.kind == "error" and "session" in result.text
