# Tasks: Session log with SQLite persistence

## Status marks
<!-- [ ] pending | [x] done | [!] BLOCKED: reason | [ ]* optional
     [-] DROPPED: <reason> — cut/superseded/wrong, gone for good
     [>] → <spec_id> — deferred/moved; a real spec now owns it (create a DRAFT if none)
     Dropped and deferred are OPPOSITE fates. A bare skip, or a [>] with no
     destination, is a dangling deferral and is FORBIDDEN (Rule 20). -->

## Tasks

- [ ] 1. Foundation
  - [ ] 1.1 Scaffold package and tooling
    - `pyproject.toml` (project `dshpy`, package at `src/dshpy`, path dep on the
      local plugkit kernel), `uv.lock` via `uv sync`, `src/dshpy/` layout,
      `.gitignore`.
    - **Depends**: —
    - **Requirements**: —
  - [ ] 1.2 `events.py` vocabulary
    - `SESSION_FORMAT_VERSION`, `SURFACE_EVENTS`, `TURN_EVENTS`,
      `STEP_EVENTS`, and the documented payload field specs for the core
      event types.
    - **Depends**: 1.1
    - **Requirements**: 4.1, 4.2, 4.3

- [ ] 2. Core
  - [ ] 2.1 `Session` — append-only log
    - `SessionEvent` (frozen dataclass), `SessionHeader`, `Session.seq`,
      immutable `events` view, `append()` with contiguous seq + surface
      bookkeeping.
    - **Depends**: 1.2
    - **Requirements**: 1.1, 1.2, 1.3, 1.5
  - [ ] 2.2 `Session.derive_messages()` — surface projection
    - Project surface events to model-visible messages; drop malformed ones
      without crashing.
    - **Depends**: 2.1
    - **Requirements**: 1.4
  - [ ] 2.3 Lossless-JSON validation at the append boundary
    - Reject cycles / `NaN` / unsupported scalars before any memory write.
    - **Depends**: 2.1
    - **Requirements**: 1.3, NF 1, NF 2
  - [ ] 2.4 `SessionStore` Service — `ctx.sessions`
    - Kernel `Service` with `provide="sessions"`; `create/get/list`;
      `session/event` lifecycle broadcast; fiber-bound disposal.
    - **Depends**: 2.1
    - **Requirements**: 2.1, 2.2, 2.3, 2.4

- [ ] 3. Persistence
  - [ ] 3.1 Persistence seam + SQLite backend
    - `SessionPersistence` ABC (`create/append/load/list`); async-stepped
      `sqlite3` WAL backend; schema per the data-architecture anchor.
    - **Depends**: 2.1
    - **Requirements**: 3.1, 3.2, 3.4
  - [ ] 3.2 Version guard + error handling
    - Refuse to load a session whose version differs; `SessionPersistenceError`
      / `SessionFormatUnsupportedError`.
    - **Depends**: 3.1
    - **Requirements**: 3.3, NF 1

- [ ] 4. Tests
  - [ ] 4.1 Test session log invariants
    - Contiguous seq, immutability, surface membership, derive projection.
    - **Depends**: 2.1, 2.2
    - **Requirements**: 1.1, 1.2, 1.4, 1.5
  - [ ] 4.2 Test lossless JSON rejection
    - Cycle / NaN / unsupported scalar → rejected, no partial append.
    - **Depends**: 2.3
    - **Requirements**: 1.3
  - [ ] 4.3 Test store lifecycle
    - `create/get/list`; `session/event` broadcast; fiber-bound disposal.
    - **Depends**: 2.4
    - **Requirements**: 2.1, 2.2, 2.3, 2.4
  - [ ] 4.4 Test persistence round-trip — the MVP proof
    - create → append → flush → close → `load` → `derive_messages` on a real
      `tmp_path` SQLite file; plus missing-session and version-mismatch.
    - **Depends**: 3.1, 3.2
    - **Requirements**: 3.1, 3.2, 3.3, 3.4, NF 2

- [ ] 5. Wrap
  - [ ] 5.1 Root README + docs index
    - What the port is, setup (`uv sync`), run tests, where each kind of truth
      lives; CLAUDE.md with `specs_root: specs` and setup pointers.
    - **Depends**: 1.1
    - **Requirements**: —
  - [ ] 5.2 Close spec — verify all gates, mark CLOSED
    - All tests green; review the Q1–Q7 bar over the diff; set `status:
      CLOSED`, `closed_as: SHIPPED`.
    - **Depends**: 4.1, 4.2, 4.3, 4.4, 5.1
    - **Requirements**: all

## Notes

- The plugkit kernel is a git worktree at
  `~/Dropbox/Projects/bayeslearner-microkernel` (remote
  `github.com/bayeslearner/plugkit`). Wire it as a path dependency; it is
  included in this repo's dev tooling, not vendored into this repo's tree.
- Persistence is the natural entry point for the runnable check: the round-trip
  test *is* the honest MVP proof, not a reflexive unit test.
