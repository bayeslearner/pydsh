"""Schedules, hooks, invariants — Requirements 1 to 5, properties 1 to 3.

Schedules are driven by *passing the clock in*, not by waiting: a test that
sleeps for a reminder is slow and still does not prove the interesting thing,
which is that delivery is decided against the durable log rather than the
timer.
"""

from __future__ import annotations

import pytest

from plugkit import Context, PointsService, ToolsService

from pydsh.capability import ShellService
from pydsh.governance import (
    MIN_EVERY_INTERVAL_SECONDS,
    HookOutput,
    HooksProtocol,
    InvariantRegistry,
    ScheduleError,
    ScheduleRuntime,
    create_after_record,
    create_change,
    create_every_record,
    decode_schedule_record,
    fired_change,
    fold_schedules,
    matches,
    merge_hook_outputs,
    parse_hook_output,
    summarize_stderr,
)
from pydsh.message import decode_payload
from pydsh.session import SessionStore

pytestmark = pytest.mark.asyncio

HOUR_MS = 3_600_000


async def build(**config) -> Context:
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    await root.plugin(ShellService)
    await root.plugin(ScheduleRuntime, config.get("schedules", {}))
    await root.plugin(HooksProtocol, config.get("hooks", {}))
    await root.plugin(InvariantRegistry)
    return root


def reminders(session) -> list[str]:
    out = []
    for message in session.derive_messages():
        decoded = decode_payload(message)
        if getattr(decoded.source, "plugin", None) == "schedule":
            out.append(decoded.content[0].text)
    return out


# --------------------------------------------------------------------------- #
# Schedule records (R1)
# --------------------------------------------------------------------------- #
async def test_a_one_shot_after_a_delay():
    record = create_after_record("check the deploy", 3600, 1_000_000)
    assert record["kind"] == "after"
    assert record["scheduled_at"] == 1_000_000 + HOUR_MS


async def test_an_empty_prompt_is_refused():
    with pytest.raises(ScheduleError) as caught:
        create_after_record("   ", 60, 1_000_000)
    assert caught.value.code == "invalid_prompt"


async def test_a_target_in_the_past_is_refused():
    """A reminder for a moment that has gone is a mistake, not an instruction."""
    from pydsh.governance import create_at_record

    with pytest.raises(ScheduleError, match="in the past"):
        create_at_record("too late", 500_000, 1_000_000)


async def test_an_interval_below_the_floor_is_refused_naming_it():
    """I2 — a one-second repeat is a busy loop with a prompt attached."""
    with pytest.raises(ScheduleError) as caught:
        create_every_record("nag me", 1, 1_000_000)
    assert caught.value.code == "frequency_too_high"
    assert str(MIN_EVERY_INTERVAL_SECONDS) in str(caught.value)


async def test_an_instant_outside_a_four_digit_year_is_refused():
    """Almost always a units mistake — seconds where milliseconds were meant."""
    from pydsh.governance import create_at_record

    with pytest.raises(ScheduleError, match="check the units"):
        create_at_record("far future", 10**18, 1_000_000)


async def test_decoding_rejects_an_unknown_kind():
    with pytest.raises(ScheduleError, match="unknown"):
        decode_schedule_record(
            {"id": "a", "kind": "someday", "prompt": "x", "scheduled_at": 2_000_000,
             "every_seconds": None}
        )


async def test_decoding_rejects_an_extra_field():
    with pytest.raises(ScheduleError, match="unexpected"):
        decode_schedule_record(
            {"id": "a", "kind": "after", "prompt": "x", "scheduled_at": 2_000_000,
             "every_seconds": None, "priority": "high"}
        )


async def test_a_one_shot_must_not_carry_an_interval():
    with pytest.raises(ScheduleError, match="must not carry"):
        decode_schedule_record(
            {"id": "a", "kind": "after", "prompt": "x", "scheduled_at": 2_000_000,
             "every_seconds": 600}
        )


# --------------------------------------------------------------------------- #
# Folding (R2) — property 3
# --------------------------------------------------------------------------- #
async def test_folding_separates_active_from_overdue():
    """R2.4 — an overdue reminder is reported, not silently dropped."""
    soon = create_after_record("soon", 3600, 1_000_000)
    past = create_after_record("past", 60, 1_000_000)
    changes = [create_change(soon), create_change(past)]

    folded = fold_schedules(changes, 1_000_000 + 120_000)
    assert [r["prompt"] for r in folded["overdue"]] == ["past"]
    assert [r["prompt"] for r in folded["active"]] == ["soon"]


async def test_a_one_shot_is_completed_by_firing():
    record = create_after_record("once", 60, 1_000_000)
    changes = [create_change(record), fired_change(record["id"], 1_100_000)]
    folded = fold_schedules(changes, 1_200_000)
    assert folded["active"] == [] and folded["overdue"] == []


async def test_a_repeating_schedule_advances_by_firing():
    record = create_every_record("often", 600, 1_000_000)
    next_at = 1_000_000 + 2 * 600_000
    changes = [create_change(record), fired_change(record["id"], 1_600_000, next_at)]

    folded = fold_schedules(changes, 1_600_000)
    assert folded["active"][0]["scheduled_at"] == next_at


async def test_folding_is_reproducible_from_the_log(tmp_path):
    """Property 3 — schedules survive a restart because they are a fold."""
    from pydsh.session import SqliteSessionPersistence

    root = await build()
    root.sessions.attach_persistence(SqliteSessionPersistence(str(tmp_path / "log.db")))
    session = root.sessions.create("chat-1")
    root.schedules.create(session, {"prompt": "outlive it", "after_seconds": 3600})
    await root.sessions.flush(session)

    reader = await build()
    reader.sessions.attach_persistence(SqliteSessionPersistence(str(tmp_path / "log.db")))
    reloaded = await reader.sessions.resume("chat-1")
    assert [r["prompt"] for r in reader.schedules.list(reloaded)] == ["outlive it"]


# --------------------------------------------------------------------------- #
# The runtime (R3) — property 1
# --------------------------------------------------------------------------- #
async def test_creating_records_a_change():
    root = await build()
    session = root.sessions.create("chat-1")
    record = root.schedules.create(session, {"prompt": "later", "after_seconds": 600})

    assert [e.type for e in session.events] == ["schedule/change"]
    assert root.schedules.list(session)[0]["id"] == record["id"]


async def test_nothing_is_delivered_before_the_durable_target():
    """Property 1 (I1) — the timer is an approximation; the log is the truth."""
    root = await build()
    session = root.sessions.create("chat-1")
    root.schedules.create(session, {"prompt": "later", "after_seconds": 3600})

    # A wake-up long before the target — a clock adjustment, a suspended laptop.
    delivered = await root.schedules.tick(session, at=root.schedules.state(session)["active"][0]["scheduled_at"] - 1)
    assert delivered == []
    assert reminders(session) == []


async def test_a_due_reminder_is_delivered_as_history():
    root = await build()
    session = root.sessions.create("chat-1")
    record = root.schedules.create(session, {"prompt": "check the deploy", "after_seconds": 600})

    delivered = await root.schedules.tick(session, at=record["scheduled_at"])
    assert len(delivered) == 1
    assert "check the deploy" in reminders(session)[0]


async def test_an_overdue_reminder_says_it_is_late():
    """R3.6 — session-local delivery, honest about it."""
    root = await build()
    session = root.sessions.create("chat-1")
    record = root.schedules.create(session, {"prompt": "nudge", "after_seconds": 600})

    await root.schedules.tick(session, at=record["scheduled_at"] + 90_000)
    assert "due 90s ago" in reminders(session)[0]


async def test_a_one_shot_does_not_fire_twice():
    root = await build()
    session = root.sessions.create("chat-1")
    record = root.schedules.create(session, {"prompt": "once", "after_seconds": 600})

    await root.schedules.tick(session, at=record["scheduled_at"])
    again = await root.schedules.tick(session, at=record["scheduled_at"] + 10_000)
    assert again == []
    assert len(reminders(session)) == 1


async def test_a_repeating_reminder_is_re_armed():
    root = await build()
    session = root.sessions.create("chat-1")
    record = root.schedules.create(session, {"prompt": "often", "every_seconds": 600})

    await root.schedules.tick(session, at=record["scheduled_at"])
    remaining = root.schedules.list(session)
    assert len(remaining) == 1
    assert remaining[0]["scheduled_at"] > record["scheduled_at"]


async def test_deleting_removes_it():
    root = await build()
    session = root.sessions.create("chat-1")
    record = root.schedules.create(session, {"prompt": "never mind", "after_seconds": 600})

    assert root.schedules.delete(session, record["id"]) is True
    assert root.schedules.list(session) == []
    assert root.schedules.delete(session, "no-such-id") is False


async def test_the_schedule_tool_reports_a_refusal_with_its_code():
    root = await build()
    session = root.sessions.create("chat-1")

    class Caller:
        pass

    caller = Caller()
    caller.session = session

    result = await root.tools.execute(
        "schedule", {"operation": "create", "prompt": "nag", "every_seconds": 1},
        caller=caller,
    )
    assert "frequency_too_high" in result.value


# --------------------------------------------------------------------------- #
# Hooks (R4) — property 2
# --------------------------------------------------------------------------- #
async def test_a_literal_matcher_is_an_alternation():
    assert matches("write|edit", "write") is True
    assert matches("write|edit", "read") is False


async def test_no_matcher_means_every_call():
    assert matches(None, "anything") is True


async def test_a_regex_matcher_is_opt_in():
    """So a tool name containing a metacharacter is not accidentally a pattern."""
    assert matches("tool.name", "toolXname") is False        # literal
    assert matches("tool.name", "toolXname", regex=True) is True


async def test_json_on_stdout_is_the_rich_form():
    output = parse_hook_output(0, '{"decision": "ask", "reason": "check with a human"}', "")
    assert output.decision == "ask"
    assert output.reason == "check with a human"


async def test_a_non_zero_exit_without_json_is_read_as_a_block():
    """R4.4 — the conservative reading, and it lets a plain script participate."""
    output = parse_hook_output(2, "", "policy says no")
    assert output.decision == "block"
    assert "policy says no" in output.reason


async def test_a_zero_exit_with_no_json_allows():
    assert parse_hook_output(0, "", "").decision is None


async def test_stderr_is_bounded_before_it_is_stored():
    """I4"""
    summary = summarize_stderr("x" * 5000, max_chars=100)
    assert len(summary) < 200
    assert "more characters" in summary


async def test_merging_is_conservative_in_any_order():
    """Property 2 (I3) — an operator's "no" must not depend on how many other
    hooks happen to be installed."""
    allow = HookOutput(decision="allow")
    block = HookOutput(decision="block", reason="policy says no")

    for outputs in (
        [block, allow, allow],
        [allow, block, allow],
        [allow, allow, block],
    ):
        merged = merge_hook_outputs(outputs)
        assert merged.decision == "block"
        assert merged.block_reason == "policy says no"


async def test_an_ask_survives_unless_something_blocks():
    assert merge_hook_outputs([HookOutput(decision="allow"), HookOutput(decision="ask")]).decision == "ask"
    assert merge_hook_outputs([HookOutput(decision="ask"), HookOutput(decision="block")]).decision == "block"


async def test_the_first_block_reason_is_the_one_reported():
    """So the outcome does not depend on the order hooks finish in."""
    merged = merge_hook_outputs([
        HookOutput(decision="block", reason="first"),
        HookOutput(decision="block", reason="second"),
    ])
    assert merged.block_reason == "first"


async def test_additional_context_accumulates_from_every_hook():
    merged = merge_hook_outputs([
        HookOutput(additional_context="one"),
        HookOutput(additional_context="two"),
    ])
    assert merged.additional_contexts == ["one", "two"]


async def test_an_input_rewrite_is_recorded_and_refused():
    """R4.6 — rewriting the call is a much larger power than approve-or-refuse."""
    merged = merge_hook_outputs([HookOutput(updated_input={"path": "/etc/passwd"})])
    assert merged.decision == "allow"
    assert any("not honoured" in w for w in merged.warnings)


async def test_a_real_hook_runs_and_blocks(tmp_path):
    root = await build()
    root.hooks.register("tools/pre", {"command": "echo 'policy says no' >&2; exit 2"})

    outcome = await root.hooks.run_point("tools/pre", "write", {"path": "/tmp/x"})
    assert outcome.decision == "block"
    assert "policy says no" in outcome.block_reason


async def test_a_real_hook_can_allow():
    root = await build()
    root.hooks.register("tools/pre", {"command": "exit 0"})
    assert (await root.hooks.run_point("tools/pre", "write", {})).decision == "allow"


async def test_a_hook_receives_its_payload():
    root = await build()
    root.hooks.register("tools/pre", {"command": "echo $PYDSH_HOOK_PAYLOAD"})
    outcome = await root.hooks.run_point("tools/pre", "write", {"path": "/tmp/x"})
    assert outcome.decision == "allow"  # it printed non-JSON and exited 0


async def test_no_matching_hook_allows():
    root = await build()
    root.hooks.register("tools/pre", {"command": "exit 2", "matcher": "read"})
    assert (await root.hooks.run_point("tools/pre", "write", {})).decision == "allow"


async def test_a_hook_that_cannot_run_blocks():
    """A broken deployment script is a decision, not a crash."""
    root = await build()
    root.hooks.register("tools/pre", {"command": "exit 127"})
    assert (await root.hooks.run_point("tools/pre", "write", {})).decision == "block"


async def test_a_hook_needs_a_command():
    root = await build()
    with pytest.raises(ValueError, match="needs a command"):
        root.hooks.register("tools/pre", {})


# --------------------------------------------------------------------------- #
# Invariants (R5)
# --------------------------------------------------------------------------- #
async def test_a_passing_invariant_is_reported():
    root = await build()
    root.invariants.register("always", "one equals one", lambda: 1 == 1)
    result = root.invariants.check()
    assert result["ok"] is True
    assert result["passed"] == ["always"]


async def test_a_failing_invariant_names_what_it_checked():
    """I5 — 'invariant 3 failed' tells an operator nothing."""
    root = await build()
    root.invariants.register(
        "attached", "every open session has a persistence backend", lambda: False
    )
    failed = root.invariants.check()["failed"]
    assert failed[0]["name"] == "attached"
    assert "persistence backend" in failed[0]["description"]


async def test_an_invariant_that_raises_is_reported_not_fatal():
    """One broken check must not hide the state of the others."""
    root = await build()
    root.invariants.register("boom", "this one is broken",
                             lambda: (_ for _ in ()).throw(RuntimeError("bad check")))
    root.invariants.register("fine", "this one works", lambda: True)

    result = root.invariants.check()
    assert result["passed"] == ["fine"]
    assert "bad check" in result["failed"][0]["reason"]


async def test_an_invariant_needs_a_description():
    root = await build()
    with pytest.raises(ValueError, match="needs a description"):
        root.invariants.register("nameless", "", lambda: True)


async def test_disposing_removes_an_invariant():
    root = await build()
    dispose = root.invariants.register("temporary", "for a while", lambda: True)
    assert root.invariants.names() == ["temporary"]
    assert dispose() is True
    assert root.invariants.names() == []
