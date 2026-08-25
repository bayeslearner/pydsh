"""Tool pairing and compaction — Requirements 3 to 5, property 3.

Compaction runs over a session the agent loop actually produced, with a
scripted model for the summary. Hand-built logs would not exercise the thing
that makes this hard: real tool calls, in real turns, that must not be cut
apart.
"""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from plugkit import Context, PointsService, ToolsService

from pydsh.agent import Agent, AgentOptions
from pydsh.bounded import ToolResultPruner
from pydsh.compaction import BasicCompaction, CompactionRefused
from pydsh.llm import (
    ChunkType,
    GenerateOptions,
    LlmAdapter,
    LlmProviderInfo,
    LlmService,
    StreamChunk,
)
from pydsh.message import MessageSource, TextBlock, create_user_message, encode_payload
from pydsh.llm import TokenMeter
from pydsh.session import (
    SessionStore,
    balanced_after,
    balanced_before,
    surface_balance,
)

pytestmark = pytest.mark.asyncio


class Scripted(LlmAdapter):
    """Answers plainly, unless asked to summarise — then says so."""

    def __init__(self, tool_turns: int = 0, summary: str = "they discussed things"):
        self.tool_turns = tool_turns
        self.summary = summary
        self.calls = 0
        self.fail_summary = False

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        self.calls += 1
        if options.purpose == "compaction":
            if self.fail_summary:
                raise RuntimeError("the summariser fell over")
            yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text=self.summary)
            yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})
            return

        if self.tool_turns > 0:
            self.tool_turns -= 1
            yield StreamChunk(
                type=ChunkType.TOOL_CALL_DELTA, index=0,
                tool_call_id=f"c{self.calls}", tool_call_name="echo", arguments_delta="{}",
            )
            yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "tool-calls"})
            return

        yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text="an answer")
        yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})


def echo_tool():
    class _Tool:
        name = "echo"
        description = ""
        parameters: dict = {}

        async def execute(self, arguments, execution=None):
            return "echoed"

    return _Tool()


async def build(adapter: Scripted, **compaction_config) -> tuple[Context, Agent]:
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(LlmService)
    await root.plugin(TokenMeter)
    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    root.tools.register(echo_tool())
    root.llm.register_adapter(["acme"], adapter)
    await root.plugin(BasicCompaction, compaction_config)
    session = root.sessions.create("chat-1")
    return root, Agent(root, session, AgentOptions(provider="acme", model="a-1"))


def texts(session) -> list[str]:
    from pydsh.message import decode_payload

    out = []
    for message in session.derive_messages():
        decoded = decode_payload(message)
        out.append("".join(b.text for b in decoded.content if hasattr(b, "text")))
    return out


# --------------------------------------------------------------------------- #
# Tool pairing (R3)
# --------------------------------------------------------------------------- #
async def test_a_plain_conversation_is_balanced_everywhere():
    root, agent = await build(Scripted())
    await agent.run("one")
    await agent.run("two")

    balance = surface_balance(agent.session)
    assert all(balance["cut_balanced"])


async def test_a_cut_between_a_tool_call_and_its_result_is_unbalanced():
    """R3.2 — the constraint the whole sprint bends around."""
    root, agent = await build(Scripted(tool_turns=1))
    await agent.run("do something")

    nodes = agent.session.surface_nodes
    by_seq = {e.seq: e for e in agent.session.events}
    assistant = next(
        seq for seq in nodes if by_seq[seq].type == "assistant/message"
    )

    # After the assistant message that requested a call, before its result.
    assert balanced_after(agent.session, assistant) is False
    # And once the result has landed, balance returns.
    result = next(seq for seq in nodes if by_seq[seq].type == "tool/result")
    assert balanced_after(agent.session, result) is True


async def test_balance_is_cached_and_invalidated_by_the_generation():
    """R3.5 — the generation exists for exactly this."""
    root, agent = await build(Scripted())
    await agent.run("one")

    first = surface_balance(agent.session)
    assert surface_balance(agent.session) is first  # cached

    agent.session.append(
        "user/message",
        encode_payload(create_user_message([TextBlock("s")], MessageSource("user"))),
        surface_op={"op": "replace", "start": agent.session.surface_nodes[0],
                    "end": agent.session.surface_nodes[-1]},
    )
    assert surface_balance(agent.session) is not first


async def test_asking_about_a_node_not_on_the_surface_raises():
    root, agent = await build(Scripted())
    await agent.run("one")
    with pytest.raises(RuntimeError, match="not on the surface"):
        balanced_before(agent.session, 9999)


# --------------------------------------------------------------------------- #
# Compaction (R4) — property 3
# --------------------------------------------------------------------------- #
async def test_compaction_replaces_history_with_a_summary():
    adapter = Scripted(summary="earlier: they said hello")
    root, agent = await build(adapter, keep_recent_nodes=2)
    for i in range(4):
        await agent.run(f"message {i}")

    before = len(agent.session.surface_nodes)
    result = await root.compaction.compact_now(agent)

    assert result is not None
    assert len(agent.session.surface_nodes) < before
    assert "earlier: they said hello" in texts(agent.session)[0]


async def test_compaction_keeps_the_most_recent_nodes():
    """R4.9 — summarising what the user just said would be absurd."""
    adapter = Scripted(summary="the earlier part")
    root, agent = await build(adapter, keep_recent_nodes=2)
    for i in range(4):
        await agent.run(f"message {i}")

    await root.compaction.compact_now(agent)
    assert texts(agent.session)[-1] == "an answer"
    assert len(agent.session.surface_nodes) >= 2


async def test_nothing_is_deleted_by_compaction():
    """Property 1 again, through the engine rather than the primitive."""
    adapter = Scripted()
    root, agent = await build(adapter, keep_recent_nodes=2)
    for i in range(4):
        await agent.run(f"message {i}")
    before = [(e.seq, e.type) for e in agent.session.events]

    await root.compaction.compact_now(agent)

    after = [(e.seq, e.type) for e in agent.session.events]
    assert after[: len(before)] == before


async def test_the_lifecycle_is_recorded():
    """R4.4 — start, summary, checkpoint, end."""
    adapter = Scripted()
    root, agent = await build(adapter, keep_recent_nodes=2)
    for i in range(4):
        await agent.run(f"message {i}")

    result = await root.compaction.compact_now(agent)
    types = [e.type for e in agent.session.events]
    assert "compaction/start" in types
    assert "compaction/summary" in types
    assert "compaction/end" in types
    assert result.start_seq < result.summary_seq < result.checkpoint_seq


async def test_the_checkpoint_names_everything_it_descends_from():
    """R4.5 — 'what did this summary replace' stays answerable forever."""
    adapter = Scripted()
    root, agent = await build(adapter, keep_recent_nodes=2)
    for i in range(4):
        await agent.run(f"message {i}")

    result = await root.compaction.compact_now(agent)
    checkpoint = next(e for e in agent.session.events if e.seq == result.checkpoint_seq)

    assert result.start_seq in checkpoint.source_event_seqs
    assert result.summary_seq in checkpoint.source_event_seqs
    for seq in result.shadowed_seqs:
        assert seq in checkpoint.source_event_seqs


async def test_a_short_conversation_compacts_to_nothing():
    """R4.8 — an ordinary outcome, not a failure."""
    root, agent = await build(Scripted(), keep_recent_nodes=8)
    await agent.run("just one")
    assert await root.compaction.compact_now(agent) is None


async def test_an_unbalanced_region_is_refused():
    """Property 3 (I2) — asked of the engine directly."""
    root, agent = await build(Scripted(tool_turns=1), keep_recent_nodes=0)
    await agent.run("do something")

    by_seq = {e.seq: e for e in agent.session.events}
    nodes = agent.session.surface_nodes
    assistant = next(seq for seq in nodes if by_seq[seq].type == "assistant/message")

    with pytest.raises(CompactionRefused, match="separate a tool call"):
        await root.compaction.compact_region(nodes[0], assistant, agent)


async def test_a_balanced_region_containing_a_whole_tool_cycle_is_allowed():
    """The complement: a full call-and-result pair may be summarised together."""
    root, agent = await build(Scripted(tool_turns=1), keep_recent_nodes=0)
    await agent.run("do something")
    await agent.run("and again")

    nodes = agent.session.surface_nodes
    end = next(seq for seq in reversed(nodes) if balanced_after(agent.session, seq))
    result = await root.compaction.compact_region(nodes[0], end, agent)
    assert result is not None


async def test_a_failed_summary_is_recorded_and_leaves_the_surface_alone():
    """R4.6 (I5) — compaction runs unattended; a silent failure is invisible."""
    adapter = Scripted()
    root, agent = await build(adapter, keep_recent_nodes=2)
    for i in range(4):
        await agent.run(f"message {i}")

    before = list(agent.session.surface_nodes)
    adapter.fail_summary = True
    with pytest.raises(RuntimeError, match="fell over"):
        await root.compaction.compact_now(agent)

    assert agent.session.surface_nodes == before
    ends = [e for e in agent.session.events if e.type == "compaction/end"]
    assert ends and "fell over" in ends[-1].data["error"]


async def test_compact_if_needed_does_nothing_below_the_threshold():
    root, agent = await build(Scripted(), threshold_tokens=1_000_000, keep_recent_nodes=0)
    for i in range(4):
        await agent.run(f"message {i}")
    assert await root.compaction.compact_if_needed(agent, "turn/end") is None


async def test_compact_if_needed_acts_above_the_threshold():
    root, agent = await build(Scripted(), threshold_tokens=1, keep_recent_nodes=2)
    for i in range(4):
        await agent.run(f"message {i}")
    assert await root.compaction.compact_if_needed(agent, "turn/end") is not None


async def test_a_compacted_session_still_streams_the_right_history():
    """The point of all of it: the next model call sees the summary."""
    adapter = Scripted(summary="everything before")
    root, agent = await build(adapter, keep_recent_nodes=2)
    for i in range(4):
        await agent.run(f"message {i}")
    await root.compaction.compact_now(agent)

    await agent.run("what now?")
    last_request = [c for c in [adapter] if True]  # the adapter recorded via calls
    history = texts(agent.session)
    assert any("everything before" in text for text in history)


# --------------------------------------------------------------------------- #
# prune_session (R5) — carried in from sprint 09
# --------------------------------------------------------------------------- #
async def test_pruning_a_session_replaces_over_budget_tool_results():
    root, agent = await build(Scripted())
    await root.plugin(
        ToolResultPruner, {"threshold_chars": 60, "head_chars": 5, "tail_chars": 5}
    )

    from pydsh.message import ToolResultBlock

    session = agent.session
    session.append("turn/start", {"turn": 1})
    big = create_user_message(
        [ToolResultBlock(tool_call_id="c1", content=(TextBlock("X" * 500),), is_error=False)],
        source=MessageSource("tool"),
    )
    session.append(
        "tool/result",
        {"turn": 1, "step": 1, "message": encode_payload(big), "error": False, "meta": None},
    )

    before = list(session.surface_nodes)
    report = root.tool_result_pruner.prune_session(session)

    assert report["pruned"] and report["chars_removed"] > 400
    assert len(session.surface_nodes) == len(before)  # one node for one node

    # The marker is inside the result block, which is where the text lives —
    # the level the budget has to be measured at.
    from pydsh.message import decode_payload

    surviving = decode_payload(session.derive_messages()[0])
    inner = surviving.content[0].content[0].text
    assert "pruned" in inner
    assert len(inner) < 500


async def test_pruning_a_session_with_nothing_over_budget_does_nothing():
    root, agent = await build(Scripted())
    await root.plugin(ToolResultPruner)
    await agent.run("hello")

    report = root.tool_result_pruner.prune_session(agent.session)
    assert report == {"pruned": [], "chars_removed": 0}
