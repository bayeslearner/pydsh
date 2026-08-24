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

A consumer mounts these seams and keeps its own domain — users, roles, cases,
workspace apps — as plugins above them. That split is the point: the core stays
general, and nothing consumer-shaped is built into it.

## Using it

pydsh installs from git; it pulls the pinned `plugkit` kernel with it.

```bash
uv add "pydsh @ git+https://github.com/bayeslearner/pydsh@v0.2.2"
```

```python
from plugkit import Context
from pydsh import LlmService, SessionStore, TokenMeter

root = Context()
await root.plugin(SessionStore)   # ctx.sessions
await root.plugin(LlmService)     # ctx.llm
await root.plugin(TokenMeter)     # ctx.token_meter
```

Mount a provider adapter of your own onto `ctx.llm` — pydsh ships none, by
design (see the coverage contract).

## Developing on it

```bash
uv sync              # resolves the pinned kernel from git
uv run pytest tests  # the suite
```

- Python ≥ 3.13, managed with `uv` (never pip/poetry).
- To work against a **local kernel checkout**, shadow the pinned copy in your
  venv — do not add a `[tool.uv.sources]` path override:

  ```bash
  uv sync && uv pip install -e ../bayeslearner-microkernel
  ```

  uv reads `pyproject.toml` out of the git checkout when a consumer installs
  from a tag, so a relative path source is resolved as a subdirectory of *this*
  repo and the install fails. v0.2.0 shipped that way and was unusable.
- `reference/` holds the upstreams this ports from (git-ignored; see
  `CLAUDE.md`).

## What works today

- **The session log** (`pydsh.session`): an append-only, immutable event log
  per conversation with lossless-JSON payloads, a derived model-visible
  message list, and a `ctx.sessions` service (create/get/list, fiber-bound
  disposal). Observer failures on the append feed are contained — a throwing
  listener cannot undo a committed append.
- **SQLite persistence**: a session survives a process restart — events flush
  to a WAL SQLite file and reload identically (`tests/test_restart.py` proves
  it across two separate processes).
- **The message vocabulary** (`pydsh.message`): immutable content blocks
  (text, reasoning, tool call, tool result), `Message`, `MessageSource`, and
  the tagged encoding that lets a message reach the session log and come back
  equal.
- **The LLM seam** (`pydsh.llm`, `ctx.llm`): an adapter registry with
  all-or-nothing route binding and releasable handles, plus a `stream()` that
  resolves the three-layer call config (provider defaults < session header <
  request) and dispatches through the `llm/stream` waterfall so middleware can
  wrap the adapter. Per-route retry policy with bounded jittered backoff, which
  never replays chunks the caller already saw.
- **Token metering** (`ctx.token_meter`): one estimator for conversation
  pressure, measured against the session surface.

No provider adapter ships here — `openai_compatible`, `deepseek` and `pi_ai`
are plugins in a later sprint. The agent loop, tools seam, and the wider
service catalogue are queued in the order
[`docs/design/service-catalogue.md`](docs/design/service-catalogue.md) defines.

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
