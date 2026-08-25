"""``ctx.jobs`` — work that outlives the tool call that started it.

A model asking for a long build should not spend its whole step budget waiting.
It starts a job, gets an id, does something else, and reads back later.

Two decisions shape everything here.

**A job is owned.** It belongs to the session that started it, and the owner is
checked on every operation — not once at creation. A job the caller does not
own is reported as **absent**, not forbidden: "you may not read job 7" confirms
that job 7 exists and belongs to someone else, which is information the caller
did not have and is not entitled to.

**Output is consumed on read.** A poller that could re-read from the beginning
would re-inject everything before it on every poll, growing the context cost
quadratically — which defeats the reason the work was backgrounded.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from abc import abstractmethod
from typing import Any, Callable, Optional

from plugkit import Service

from ..cancel import CancelSignal

logger = logging.getLogger("pydsh.jobs")

#: Where a job can be. The last three are terminal.
JOB_STATUSES = ("running", "stopping", "completed", "killed", "failed")
TERMINAL_STATUSES = frozenset({"completed", "killed", "failed"})

#: What a job can be. Only ``bash`` runs here; ``subagent`` is in the
#: vocabulary because the kind is part of the contract, and the sprint that
#: ports child sessions implements it.
JOB_KINDS = ("bash", "subagent")

#: Bytes of output one job may buffer before the oldest is dropped. Bounded
#: because a job nobody reads must not grow without limit.
DEFAULT_MAX_BUFFER_BYTES = 1_048_576


class JobNotFound(LookupError):
    """No such job — or none this caller owns. Deliberately the same answer."""


class JobRegistry(Service):
    """The seam. A consumer that runs work elsewhere implements this."""

    provide = "jobs"

    @abstractmethod
    async def start(self, spec: dict, owner: Any) -> str: ...

    @abstractmethod
    def list(self, owner: Any) -> list[dict]: ...

    @abstractmethod
    def get(self, id: str, owner: Any) -> dict: ...

    @abstractmethod
    def read(self, id: str, owner: Any) -> dict: ...

    @abstractmethod
    async def kill(self, id: str, owner: Any, reason: Optional[str] = None) -> str: ...

    @abstractmethod
    async def wait(self, id: str, timeout_ms: float, owner: Any) -> dict: ...


class _Job:
    """One background job and everything known about it."""

    __slots__ = ("id", "kind", "command", "owner_id", "status", "detail",
                 "_buffer", "_task", "_settled", "signal")

    def __init__(self, id: str, kind: str, command: str, owner_id: str) -> None:
        self.id = id
        self.kind = kind
        self.command = command
        self.owner_id = owner_id
        self.status = "running"
        self.detail: Optional[str] = None
        self._buffer = ""
        self._task: Optional[asyncio.Future] = None
        self._settled = asyncio.Event()
        # How this job is stopped. Cancelling the asyncio task would not do
        # it: a cancelled task abandons the `await`, and the subprocess keeps
        # running — the same defect the shell seam fixes with a process-group
        # kill. Aborting this signal is what reaches that code.
        self.signal = CancelSignal()

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "command": self.command,
            "status": self.status,
            "detail": self.detail,
        }

    def settle(self, status: str, detail: Optional[str] = None) -> None:
        """Move to a terminal status, once. Later attempts are ignored (I2)."""
        if self.status in TERMINAL_STATUSES:
            return
        self.status = status
        self.detail = detail
        self._settled.set()

    def append_output(self, text: str, max_bytes: int) -> None:
        self._buffer += text
        if len(self._buffer.encode("utf-8")) > max_bytes:
            # Keep the tail: a job nobody read must not grow without limit, and
            # the end is where a command says how it went.
            self._buffer = self._buffer[-max_bytes:]

    def drain(self) -> str:
        text, self._buffer = self._buffer, ""
        return text


class LocalJobs(JobRegistry):
    """Runs ``bash`` jobs through ``ctx.shell``, in this process."""

    inject = ["shell"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        self._max_buffer = int(config.get("max_buffer_bytes", DEFAULT_MAX_BUFFER_BYTES))
        self._jobs: dict[str, _Job] = {}
        self._done_listeners: list[Callable[[dict], None]] = []
        self._changed_listeners: list[Callable[[], None]] = []
        ctx.effect(lambda: self._shutdown)

    # -- ownership --------------------------------------------------------- #
    @staticmethod
    def _owner_id(owner: Any) -> str:
        """A caller's identity for fencing. A missing owner owns nothing."""
        session = getattr(owner, "session", None) or owner
        return getattr(session, "id", None) or getattr(owner, "id", None) or ""

    def _owned(self, id: str, owner: Any) -> _Job:
        """The job, if this caller owns it.

        Absent rather than forbidden: telling a caller that a job exists but
        belongs to someone else is a cross-tenant leak in an error message.
        """
        job = self._jobs.get(id)
        if job is None or job.owner_id != self._owner_id(owner):
            raise JobNotFound(f"no job {id!r}")
        return job

    # -- lifecycle --------------------------------------------------------- #
    async def start(self, spec: dict, owner: Any = None) -> str:
        kind = spec.get("kind", "bash")
        if kind not in JOB_KINDS:
            raise ValueError(f"unknown job kind {kind!r}; expected one of {JOB_KINDS}")
        if kind != "bash":
            raise NotImplementedError(
                f"the {kind!r} job kind needs child sessions, which are not ported yet"
            )
        command = spec.get("command", "")
        if not command.strip():
            raise ValueError("a job needs a command")

        job = _Job(uuid.uuid4().hex[:8], kind, command, self._owner_id(owner))
        self._jobs[job.id] = job
        job._task = asyncio.ensure_future(self._run(job, spec))
        self._notify_changed()
        return job.id

    async def _run(self, job: _Job, spec: dict) -> None:
        try:
            result = await self.ctx.shell.execute(
                job.command,
                cwd=spec.get("cwd"),
                timeout_ms=spec.get("timeout_ms"),
                signal=job.signal,
            )
            job.append_output(result["stdout"], self._max_buffer)
            if result["stderr"]:
                job.append_output(f"\n[stderr]\n{result['stderr']}", self._max_buffer)
            if job.status == "stopping":
                job.settle("killed", "stopped on request")
            elif result["timed_out"]:
                job.settle("failed", "the command timed out")
            elif result["exit_code"] == 0:
                job.settle("completed")
            else:
                job.settle("failed", f"exit code {result['exit_code']}")
        except asyncio.CancelledError:
            job.settle("killed", "cancelled")
            raise
        except Exception as error:  # noqa: BLE001 - a job failing is data
            job.settle("failed", f"{type(error).__name__}: {error}")
        finally:
            self._announce_done(job)
            self._notify_changed()

    # -- reads ------------------------------------------------------------- #
    def list(self, owner: Any = None) -> list[dict]:
        """Only what this caller owns (I1)."""
        wanted = self._owner_id(owner)
        return [j.snapshot() for j in self._jobs.values() if j.owner_id == wanted]

    def get(self, id: str, owner: Any = None) -> dict:
        return self._owned(id, owner).snapshot()

    def read(self, id: str, owner: Any = None) -> dict:
        """What has been produced since the last read (I3)."""
        job = self._owned(id, owner)
        return {**job.snapshot(), "output": job.drain()}

    async def kill(self, id: str, owner: Any = None, reason: Optional[str] = None) -> str:
        job = self._owned(id, owner)
        if job.status in TERMINAL_STATUSES:
            # A no-op, not an error: the caller wanted it stopped and it is.
            return job.status
        job.status = "stopping"
        # Abort the signal rather than cancel the task: the shell seam turns
        # this into a process-group kill, so the work actually stops.
        job.signal.abort(reason or "the job was killed")
        if job._task is not None and not job._task.done():
            try:
                await job._task
            except Exception:  # noqa: BLE001 - already recorded on the job
                pass
        job.settle("killed", reason)
        self._notify_changed()
        return job.status

    async def wait(self, id: str, timeout_ms: float, owner: Any = None) -> dict:
        job = self._owned(id, owner)
        try:
            await asyncio.wait_for(job._settled.wait(), timeout=timeout_ms / 1000.0)
            return {**job.snapshot(), "timed_out": False}
        except asyncio.TimeoutError:
            return {**job.snapshot(), "timed_out": True}

    # -- notifications ----------------------------------------------------- #
    def on_job_done(self, listener: Callable[[dict], None]) -> Callable[[], None]:
        self._done_listeners.append(listener)
        return lambda: self._remove(self._done_listeners, listener)

    def on_jobs_changed(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._changed_listeners.append(listener)
        return lambda: self._remove(self._changed_listeners, listener)

    @staticmethod
    def _remove(listeners: list, listener: Any) -> None:
        for index, candidate in enumerate(listeners):
            if candidate is listener:
                listeners.pop(index)
                return

    def _announce_done(self, job: _Job) -> None:
        for listener in list(self._done_listeners):
            try:
                listener(job.snapshot())
            except Exception as exc:  # noqa: BLE001
                logger.warning("jobs: a done listener failed: %s", exc, exc_info=exc)

    def _notify_changed(self) -> None:
        for listener in list(self._changed_listeners):
            try:
                listener()
            except Exception as exc:  # noqa: BLE001
                logger.warning("jobs: a change listener failed: %s", exc, exc_info=exc)

    def _shutdown(self) -> None:
        """Unmount: stop everything still running (R1.10)."""
        for job in self._jobs.values():
            job.signal.abort("the job registry was unmounted")
            if job._task is not None and not job._task.done():
                job._task.cancel()
            job.settle("killed", "the job registry was unmounted")


__all__ = [
    "JobRegistry",
    "LocalJobs",
    "JobNotFound",
    "JOB_STATUSES",
    "TERMINAL_STATUSES",
    "JOB_KINDS",
    "DEFAULT_MAX_BUFFER_BYTES",
]
