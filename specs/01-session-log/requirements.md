---
spec_id: 01-session-log
status: ACTIVE
closed_as: null
since: 2026-08-24
until: null
epic: core
features: [session-log, session-persistence]
supersedes: []
superseded_by: null
depends_on: []
anchors: [data-architecture, service-catalogue]
---

# Session log with SQLite persistence

<!-- The YAML above is the single source of truth for status and
     relationships. Never edit it outside /spec-plan. -->
<!-- A spec is a sprint: ONE status for the whole spec (DRAFT → ACTIVE → CLOSED),
     worked in numeric order, one sprint in flight. -->

# 1 · Requirements

## Introduction

This is the first sprint of a Python port of DeepSeek Harness's service layer,
built on the plugkit kernel (the port of Cordis that the TypeScript original's
documentation describes accurately). The port's long-term target is to replace
the backends of **prismi3-agent** and **SAW**; those repos' own sprints own the
retirement, this repo owns the service layer.

This sprint builds the foundation every other service projects from: the
event-sourced session log (the `core/session` seam of the reference) plus its
SQLite persistence. It delivers one real, testable capability on its own: a
session survives a restart — events are appended, flushed to SQLite, replayed
on load, and `derive_messages()` reconstructs the same model history.

## Glossary

- **Session**: the append-only event log for one agent interaction, plus an
  in-memory projection. The source of truth for that conversation. A plain
  class, not a kernel Service.
- **SessionEvent**: one immutable entry in the log — `type`, `seq`, `time`,
  `data`.
- **Surface**: the ordered sequence of model-visible messages (`user/message`,
  `assistant/message`, `tool/result`) derived from the log.
- **SessionStore**: the kernel `Service` at `ctx.sessions` that creates, holds,
  and looks up live `Session` objects.
- **Persistence backend**: the seam that flushes events to disk and reloads
  them; this sprint implements the SQLite backend.
- **plugkit**: the kernel this is built on; a Python port of Cordis.

## Mental Model & Invariants

The user (owner) sees this as "an agent conversation that must not be lost
when the process restarts, and whose full history can be replayed."

- The session log is **append-only** and **immutable** — events are never
  edited or deleted; sequence numbers stay contiguous.
- **Model-visible means logged.** Anything that reaches a model request must be
  reconstructable from the log. This is the port's core invariant and the
  reason every future service (loop, tools, compaction) is a projection or
  listener of this log rather than a separate store.
- **The log is the single source of truth.** The in-memory surface and the
  in-memory session list are derived and rebuilt from the log; never persisted
  as separate truths.
- A session's durability checkpoint is explicit: `append` mutates memory, and a
  separate `flush` awaits the write to SQLite.
- Events and their `data` payloads are **lossless JSON** — round-trips
  byte-identical and rejects invalid input at the source.
- A survives-restart session reproduces the **identical** event list and an
  equivalent `derive_messages()` after load.

## Decisions & Corrections (log)

- 2026-08-24 — Owner directed: SQLite-backed persistence (not JSONL, the
  reference's default), with prismi3-agent and SAW as the adoption target.
  Scope of sprint 01 is the session log + persistence only; the loop, tools,
  and adapters are later sprints.
- 2026-08-24 — Session log is the only store in this sprint, so persistence is
  a specific SQLite table, not a generic KV backend.

## Dev Environment (config-as-code)

- Python/deps: `pyproject.toml` + `uv` (the kernel builds with uv)
- Kernel under test: `~/Dropbox/Projects/bayeslearner-microkernel` (not yet
  published; see tasks for the dependency wiring)
- Commands: `uv run pytest`, `uv run` (no Makefile yet; add when the app grows a
  run target)

## Requirements

### Requirement 1: Session event log
**User Story:** As an agent runtime, I want an append-only event log per
conversation, so that the full history is immutable and replayable.
#### Acceptance Criteria
1. WHEN a `Session` is created, THE log SHALL begin empty with `seq == 0`.
2. WHEN `session.append(type, data)` is called, THE `Session` SHALL append an
   event with the next contiguous `seq`, a monotonic `time`, and the given
   `type`/`data`, and SHALL expose the events in order.
3. WHEN `session.append` is called with `data` that is not lossless-JSON
   serializable (a cycle, a non-finite number, a non-supported scalar), THE
   call SHALL reject the event (no partial append, no memory write).
4. WHEN `derive_messages()` is called, THE `Session` SHALL return the ordered
   model-visible messages derived from surface events (`user/message`,
   `assistant/message` → its `message`, `tool/result` → its `message`).
5. WHEN a non-surface event (e.g. `turn/start`, `assistant/chunk`) is appended,
   THE `Session` SHALL record it in the log but SHALL NOT add it to the
   surface.

### Requirement 2: SessionStore service
**User Story:** As an application, I want a `ctx.sessions` service, so that
sessions are created and looked up by id.
#### Acceptance Criteria
1. WHEN `SessionStore` is mounted, THE `ctx.sessions` name SHALL resolve to it.
2. WHEN `ctx.sessions.create(id?)` is called, THE store SHALL create a
   `Session`, register it under its id, publish it through the `session/event`
   lifecycle, and bind it to the calling fiber (so it is disposed when the
   fiber unloads).
3. WHEN `ctx.sessions.get(id)` is called, THE store SHALL return the live
   session or `None`.
4. WHEN `ctx.sessions.list()` is called, THE store SHALL return all live
   sessions.

### Requirement 3: SQLite persistence
**User Story:** As an operator, I want sessions to survive a process restart,
so that an interrupted run can be resumed with its full history.
#### Acceptance Criteria
1. WHEN a session is created AND the SQLite backend is attached AND `flush` is
   awaited, THE session's header SHALL be written to `sessions` and each
   appended event SHALL be written to `events` (one transaction).
2. WHEN `load(session_id)` reads a stored session, THE backend SHALL
   reconstruct a `Session` whose header and event list exactly match what was
   flushed.
3. WHEN a stored session's format version is not `SESSION_FORMAT_VERSION`, THE
   backend SHALL refuse to load it (no migration, no silent skip).
4. WHEN appends happen between flushes, THE in-memory log SHALL hold them; a
   subsequent `flush` SHALL persist them in order.

### Requirement 4: Event vocabulary
**User Story:** As the port, I want the reference session event vocabulary, so
that a later agent loop and the conformance suite share the reference's event
names.
#### Acceptance Criteria
1. THE module SHALL declare the core `SessionEventMap` types: `turn/start`,
   `turn/end`, `step/start`, `step/end`, `user/message`, `assistant/chunk`,
   `assistant/message`, `tool/call`, `tool/result`.
2. THE surface event set SHALL be exactly `user/message`, `assistant/message`,
   `tool/result`.
3. THE base `SessionEvent` envelope SHALL carry `type`, `seq`, `time`, `data`,
   and SHALL carry optional surface metadata (`surfaceOp`,
   `sourceEventSeqs`) only on surface events.

### Non-Functional

**NF 1**: Persistence is durable: an acknowledged `flush` is on disk (WAL +
`fsync` at commit), never only in an SQLite cache.
**NF 2**: The event log and payloads are lossless JSON; round-trip is
byte-identical.
**NF 3**: The implementation keeps the reference's semantics (event names,
dispatch modes) so plugkit's `test_conformance.py` approach — asserting against
the TS source — can be extended here.

## Out of Scope

- The agent loop, model adapters, and tool registry (later sprints; they are
  listeners/projections of this log, per the Mental Model).
- The surface/replacement layer's compaction (`surfaceOp: replace`) — the raw
  log's surface projection is sufficient until compaction exists. Surface
  metadata fields are defined but only `append` is exercised now.
- Forking (`ctx.sessions.fork`) and subagent lineage — later sprint.
- The prismi3-agent / SAW backend retirement — owned by those repos.
