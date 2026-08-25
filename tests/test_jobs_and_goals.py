"""Jobs and goals — Requirements 1 to 5, properties 1 to 3.

Jobs run real background processes. Goals are folded from a real session log.
The properties that matter are ownership (a fence, checked every time),
draining (output consumed once), and compare-and-set (two writers, one winner).
"""

from __future__ import annotations

import asyncio

import pytest

from plugkit import Context, PointsService, ToolsService

from pydsh.capability import ShellService
from pydsh.session import SessionProjections, SessionStore
from pydsh.work import (
    GoalError,
    GoalService,
    GoalTool,
    JobNotFound,
    JobTools,
    LocalJobs,
    apply_goal_change,
    create_change,
    decode_goal_change,
    empty_goal_state,
    fold_goals,
    next_change,
)

pytestmark = pytest.mark.asyncio


class Owner:
    """A stand-in for an agent: what the fence reads is `session.id`."""

    def __init__(self, session) -> None:
        self.session = session
        self.id = session.id


#: Contexts built by a test, torn down after it. A background job outlives the
#: test that started it unless something stops it, and a subprocess still
#: running when the event loop closes is orphaned — which shows up as the whole
#: suite hanging rather than as one test failing.
_BUILT: list[Context] = []


@pytest.fixture(autouse=True)
async def _stop_jobs_after_each_test():
    yield
    for context in _BUILT:
        registry = getattr(context, "jobs", None)
        if registry is None:
            continue
        for owner_id in list(registry._jobs):
            job = registry._jobs[owner_id]
            job.signal.abort("test teardown")
        for job in list(registry._jobs.values()):
            if job._task is not None and not job._task.done():
                try:
                    await asyncio.wait_for(job._task, timeout=5)
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    job._task.cancel()
    _BUILT.clear()


async def build(**config) -> Context:
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(SessionProjections)
    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    await root.plugin(ShellService)
    await root.plugin(LocalJobs, config.get("jobs", {}))
    await root.plugin(GoalService, config.get("goals", {}))
    await root.plugin(JobTools)
    await root.plugin(GoalTool)
    _BUILT.append(root)
    return root


# --------------------------------------------------------------------------- #
# Jobs (R1)
# --------------------------------------------------------------------------- #
async def test_a_job_starts_and_settles():
    root = await build()
    owner = Owner(root.sessions.create("chat-1"))

    job_id = await root.jobs.start({"command": "echo done"}, owner)
    result = await root.jobs.wait(job_id, 5000, owner)

    assert result["timed_out"] is False
    assert result["status"] == "completed"
    assert "done" in root.jobs.read(job_id, owner)["output"]


async def test_a_failing_job_settles_as_failed():
    root = await build()
    owner = Owner(root.sessions.create("chat-1"))
    job_id = await root.jobs.start({"command": "exit 7"}, owner)
    result = await root.jobs.wait(job_id, 5000, owner)
    assert result["status"] == "failed"
    assert "7" in result["detail"]


async def test_starting_needs_a_command():
    root = await build()
    owner = Owner(root.sessions.create("chat-1"))
    with pytest.raises(ValueError, match="needs a command"):
        await root.jobs.start({"command": "  "}, owner)


async def test_an_unported_job_kind_says_so_rather_than_pretending():
    root = await build()
    owner = Owner(root.sessions.create("chat-1"))
    with pytest.raises(NotImplementedError, match="child sessions"):
        await root.jobs.start({"kind": "subagent", "command": "x"}, owner)


# --------------------------------------------------------------------------- #
# Property 2 — ownership is a fence
# --------------------------------------------------------------------------- #
async def test_another_session_cannot_see_or_touch_a_job():
    """Property 2 (I1) — and it is told the job is *absent*, not forbidden."""
    root = await build()
    mine = Owner(root.sessions.create("chat-1"))
    theirs = Owner(root.sessions.create("chat-2"))

    job_id = await root.jobs.start({"command": "sleep 5"}, mine)
    await root.jobs.wait(job_id, 50, mine)  # let it start

    assert root.jobs.list(theirs) == []
    for call in (
        lambda: root.jobs.get(job_id, theirs),
        lambda: root.jobs.read(job_id, theirs),
    ):
        with pytest.raises(JobNotFound) as caught:
            call()
        # Deliberately uninformative: "you may not read job 7" confirms job 7
        # exists and belongs to someone else.
        assert "no job" in str(caught.value)
        assert "forbidden" not in str(caught.value).lower()

    with pytest.raises(JobNotFound):
        await root.jobs.kill(job_id, theirs)

    await root.jobs.kill(job_id, mine)


async def test_listing_shows_only_your_own():
    root = await build()
    mine = Owner(root.sessions.create("chat-1"))
    theirs = Owner(root.sessions.create("chat-2"))

    await root.jobs.start({"command": "echo a"}, mine)
    await root.jobs.start({"command": "echo b"}, theirs)
    await asyncio.sleep(0.2)

    assert len(root.jobs.list(mine)) == 1
    assert len(root.jobs.list(theirs)) == 1


# --------------------------------------------------------------------------- #
# Property 3 — output is consumed once
# --------------------------------------------------------------------------- #
async def test_output_is_drained_on_read():
    """Property 3 (I3) — a re-readable buffer grows the context quadratically."""
    root = await build()
    owner = Owner(root.sessions.create("chat-1"))

    job_id = await root.jobs.start({"command": "echo first"}, owner)
    await root.jobs.wait(job_id, 5000, owner)

    first = root.jobs.read(job_id, owner)["output"]
    second = root.jobs.read(job_id, owner)["output"]

    assert "first" in first
    assert second == ""


# --------------------------------------------------------------------------- #
# Killing and unmounting (R1.7, R1.10)
# --------------------------------------------------------------------------- #
async def test_killing_stops_a_running_job():
    root = await build()
    owner = Owner(root.sessions.create("chat-1"))
    job_id = await root.jobs.start({"command": "sleep 20"}, owner)
    await asyncio.sleep(0.1)

    assert await root.jobs.kill(job_id, owner) == "killed"
    assert root.jobs.get(job_id, owner)["status"] == "killed"


async def test_killing_a_finished_job_is_a_no_op():
    """The caller wanted it stopped, and it is. An error would be noise."""
    root = await build()
    owner = Owner(root.sessions.create("chat-1"))
    job_id = await root.jobs.start({"command": "echo done"}, owner)
    await root.jobs.wait(job_id, 5000, owner)

    assert await root.jobs.kill(job_id, owner) == "completed"


async def test_a_terminal_job_never_changes_again():
    """I2"""
    root = await build()
    owner = Owner(root.sessions.create("chat-1"))
    job_id = await root.jobs.start({"command": "echo done"}, owner)
    await root.jobs.wait(job_id, 5000, owner)

    await root.jobs.kill(job_id, owner)
    assert root.jobs.get(job_id, owner)["status"] == "completed"


async def test_waiting_can_time_out():
    root = await build()
    owner = Owner(root.sessions.create("chat-1"))
    job_id = await root.jobs.start({"command": "sleep 20"}, owner)

    result = await root.jobs.wait(job_id, 100, owner)
    assert result["timed_out"] is True
    assert result["status"] == "running"
    await root.jobs.kill(job_id, owner)


async def test_a_done_listener_hears_about_it():
    root = await build()
    owner = Owner(root.sessions.create("chat-1"))
    seen = []
    root.jobs.on_job_done(lambda snapshot: seen.append(snapshot["status"]))

    job_id = await root.jobs.start({"command": "echo done"}, owner)
    await root.jobs.wait(job_id, 5000, owner)
    assert seen == ["completed"]


# --------------------------------------------------------------------------- #
# The jobs tools (R2)
# --------------------------------------------------------------------------- #
async def test_the_job_tools_are_fenced_by_the_caller():
    root = await build()
    mine = Owner(root.sessions.create("chat-1"))
    theirs = Owner(root.sessions.create("chat-2"))

    started = await root.tools.execute("job_start", {"command": "echo hi"}, caller=mine)
    job_id = started.value.split()[2].rstrip(".")

    listed = await root.tools.execute("job_list", {}, caller=theirs)
    assert listed.value == "No background jobs."

    read = await root.tools.execute("job_read", {"id": job_id}, caller=theirs)
    assert read.value.startswith("Error:")


async def test_a_job_tool_without_a_caller_is_refused():
    root = await build()
    result = await root.tools.execute("job_start", {"command": "echo hi"})
    assert result.value.startswith("Error:")


# --------------------------------------------------------------------------- #
# Goal changes (R3) — property 1
# --------------------------------------------------------------------------- #
async def test_a_well_formed_change_decodes():
    change = create_change("ship the port")
    assert decode_goal_change(change)["operation"] == "create"


async def test_an_unknown_operation_is_refused():
    with pytest.raises(GoalError, match="operation"):
        decode_goal_change({"version": 1, "operation": "invent", "goal": {}})


async def test_a_wrong_version_is_refused_rather_than_guessed():
    change = {**create_change("x"), "version": 99}
    with pytest.raises(GoalError, match="refusing to guess"):
        decode_goal_change(change)


async def test_an_extra_field_is_refused():
    """A change carrying an unknown field was written against another contract."""
    change = create_change("x")
    change["goal"]["priority"] = "high"
    with pytest.raises(GoalError, match="unexpected priority"):
        decode_goal_change(change)


async def test_a_missing_field_is_refused():
    change = create_change("x")
    del change["goal"]["status"]
    with pytest.raises(GoalError, match="missing status"):
        decode_goal_change(change)


async def test_empty_text_is_refused():
    change = create_change("x")
    change["goal"]["text"] = "   "
    with pytest.raises(GoalError, match="non-empty"):
        decode_goal_change(change)


async def test_an_update_before_creation_is_refused():
    """A goal updated before it existed means the clock or the writer is wrong."""
    change = create_change("x")
    change["goal"]["updated_at"] = change["goal"]["created_at"] - 1
    with pytest.raises(GoalError, match="before created_at"):
        decode_goal_change(change)


async def test_a_create_must_start_at_revision_one():
    change = create_change("x")
    change["goal"]["revision"] = 5
    with pytest.raises(GoalError, match="revision 1"):
        apply_goal_change(empty_goal_state(), change)


async def test_two_live_goals_are_refused():
    """"What are we doing" must not have two answers."""
    state = apply_goal_change(empty_goal_state(), create_change("first"))
    with pytest.raises(GoalError) as caught:
        apply_goal_change(state, create_change("second"))
    assert caught.value.code == "GOAL_ALREADY_ACTIVE"


async def test_a_new_goal_after_completion_is_allowed():
    state = apply_goal_change(empty_goal_state(), create_change("first"))
    state = apply_goal_change(state, next_change(state["current"], "complete"))
    state = apply_goal_change(state, create_change("second"))
    assert state["current"]["text"] == "second"
    assert len(state["history"]) == 3


async def test_two_writers_cannot_both_win():
    """Property 1 (I4) — compare-and-set, without a lock."""
    state = apply_goal_change(empty_goal_state(), create_change("goal"))
    current = state["current"]

    first = next_change(current, "edit", "writer A")
    second = next_change(current, "edit", "writer B")  # same predecessor

    state = apply_goal_change(state, first)
    assert state["current"]["text"] == "writer A"

    with pytest.raises(GoalError) as caught:
        apply_goal_change(state, second)
    assert caught.value.code == "GOAL_STALE_REVISION"
    assert "re-read and try again" in str(caught.value)


async def test_folding_a_log_reproduces_the_goal():
    changes = []
    state = apply_goal_change(empty_goal_state(), create_change("one"))
    changes.append(state["history"][-1])
    for operation in ("pause", "resume", "complete"):
        change = next_change(state["current"], operation)
        state = apply_goal_change(state, change)
        changes.append(change)

    assert fold_goals(changes)["current"]["status"] == "completed"


# --------------------------------------------------------------------------- #
# The goals service (R4)
# --------------------------------------------------------------------------- #
async def test_a_goal_lands_on_the_session_log():
    root = await build()
    session = root.sessions.create("chat-1")

    goal = root.goals.set(session, "ship the port")
    assert goal["revision"] == 1
    assert [e.type for e in session.events] == ["goal/change"]
    assert root.goals.current(session)["text"] == "ship the port"


async def test_setting_again_edits_rather_than_replacing():
    root = await build()
    session = root.sessions.create("chat-1")
    root.goals.set(session, "first")
    goal = root.goals.set(session, "second")

    assert goal["revision"] == 2
    assert goal["text"] == "second"


async def test_a_goal_survives_a_reload(tmp_path):
    from pydsh.session import SqliteSessionPersistence

    root = await build()
    root.sessions.attach_persistence(SqliteSessionPersistence(str(tmp_path / "log.db")))
    session = root.sessions.create("chat-1")
    root.goals.set(session, "outlive the restart")
    await root.sessions.flush(session)

    reader = await build()
    reader.sessions.attach_persistence(SqliteSessionPersistence(str(tmp_path / "log.db")))
    reloaded = await reader.sessions.resume("chat-1")
    assert reader.goals.current(reloaded)["text"] == "outlive the restart"


async def test_a_rejected_change_leaves_no_trace():
    """The log must not contain events that were never legal."""
    root = await build()
    session = root.sessions.create("chat-1")
    root.goals.set(session, "first")

    with pytest.raises(GoalError):
        root.goals.apply(session, create_change("second"))
    assert len([e for e in session.events if e.type == "goal/change"]) == 1


async def test_the_goal_projection_tracks_the_current_goal():
    root = await build()
    session = root.sessions.create("chat-1")
    root.goals.set(session, "watched")

    value = root.session_projections.snapshot(session)["values"]["goal"]
    assert value["current"]["text"] == "watched"
    assert value["revision"] == 1


async def test_arming_is_never_written_to_the_log():
    """I6 — a restart must not silently resume driving."""
    root = await build()
    session = root.sessions.create("chat-1")
    root.goals.set(session, "drive me")

    root.goals.arm(session)
    assert root.goals.is_armed(session) is True
    assert [e.type for e in session.events] == ["goal/change"]  # nothing added

    root.goals.disarm(session)
    assert root.goals.is_armed(session) is False


async def test_arming_is_bounded():
    """R4.5 — a loop that has run hundreds of rounds is usually stuck."""
    root = await build(goals={"max_goal_rounds": 2})
    session = root.sessions.create("chat-1")
    root.goals.set(session, "drive me")

    root.goals.arm(session)
    root.goals.arm(session)
    with pytest.raises(GoalError) as caught:
        root.goals.arm(session)
    assert caught.value.code == "GOAL_ROUNDS_EXHAUSTED"


# --------------------------------------------------------------------------- #
# The goal tool (R5)
# --------------------------------------------------------------------------- #
async def test_the_goal_tool_sets_and_transitions():
    root = await build()
    owner = Owner(root.sessions.create("chat-1"))

    result = await root.tools.execute(
        "goal", {"operation": "set", "text": "ship it"}, caller=owner
    )
    assert "revision 1" in result.value

    result = await root.tools.execute("goal", {"operation": "complete"}, caller=owner)
    assert "completed" in result.value


async def test_the_goal_tool_reports_a_rejection_with_its_code():
    """R5.3 — 'stale revision' and 'already active' call for different retries."""
    root = await build()
    owner = Owner(root.sessions.create("chat-1"))
    result = await root.tools.execute("goal", {"operation": "complete"}, caller=owner)
    assert "GOAL_NOT_FOUND" in result.value


async def test_the_goal_tool_needs_text_to_set():
    root = await build()
    owner = Owner(root.sessions.create("chat-1"))
    result = await root.tools.execute("goal", {"operation": "set"}, caller=owner)
    assert result.value.startswith("Error:")
