# pydsh

A Python port of the [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
service layer, built on the **plugkit** kernel (the Python port of Cordis that the
TypeScript original's documentation describes accurately).

The port targets **100% coverage of the dsh-python service surface** — the
reference's three seams (Session, Agent, LLM), the support services, and the
app layer — except where plugkit already ships the piece (the tool registry and
event dispatch) or where a layer is consumer-domain (auth/roles, domain cases,
workspace apps) and does not belong in a general core. Coverage classes are
defined in [`docs/design/service-catalogue.md`](docs/design/service-catalogue.md).

Adoption targets: the backends of **prismi3-agent** and **SAW** will be
refactored onto this port. Their users/roles/cases stay their own plugins above
the general seams.

## Setup

```bash
uv sync              # install deps + the plugkit kernel (path dependency)
uv run pytest tests  # run the suite
```

- Python ≥ 3.13, managed with `uv` (never pip/poetry).
- The kernel is a local checkout at `../bayeslearner-microkernel`, wired as a
  path dependency until it gets a tagged release.

## What works today

- **The session log** (`pydsh.sessions`): an append-only, immutable event log
  per conversation with lossless-JSON payloads, a derived model-visible
  message list, and a `ctx.sessions` service (create/get/list, fiber-bound
  disposal).
- **SQLite persistence**: a session survives a process restart — events flush
  to a WAL SQLite file and reload identically (`tests/test_restart.py` proves
  it across two separate processes).

The agent loop, LLM adapters, and the wider service catalogue are queued as
later sprints, in the order the service catalogue defines.

## Where each kind of truth lives

- `specs/NN-name/` — sprint specs (the active sprint is the head of this dir).
- `docs/steering/pillars.md` — product health across dimensions.
- `docs/design/` — stabilized cross-cutting design + anchor docs
  (`data-architecture.md` for storage lifecycle, `service-catalogue.md` for
  coverage).
- `docs/history/` — dated review sets and research notes.
- `src/pydsh/` — the package under development.
- `tests/` — pytest; `test_restart.py` is the persistence MVP proof.

## Development

Follow `CLAUDE.md` and the spec-driven workflow. Every piece of work traces to
a spec task; tests pass before work is reported done. `uv run pytest tests -q`
is the gate.

MIT.
