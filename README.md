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
from plugkit import Context, PointsService, ToolsService
from pydsh import AgentLoop, AgentOptions, AgentRegistry, LlmService, SessionStore, TokenMeter

root = Context()
await root.plugin(SessionStore)   # ctx.sessions
await root.plugin(LlmService)     # ctx.llm
await root.plugin(TokenMeter)     # ctx.token_meter
await root.plugin(PointsService)  # ctx.points  — plugkit
await root.plugin(ToolsService)   # ctx.tools   — plugkit, optional
await root.plugin(AgentRegistry)  # ctx.agents
await root.plugin(AgentLoop)      # ctx.agent_loop

session = root.sessions.create()
agent = root.agents.create_agent(session, AgentOptions(provider="acme", model="a-1"))
await agent.run("what changed today?")
```

Order matters only in that `AgentLoop` requires `agents`, `sessions` and `llm`:
plugkit gates activation on those, so a loop mounted early stays pending until
they arrive rather than coming up half-working. `ToolsService` is optional —
without it the model is offered no tools.

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
- **The agent loop** (`pydsh.agent`, `ctx.agents` / `ctx.agent_loop`): the
  turn/step machine that drives a conversation — a model call per step, the
  tool calls it asks for executed through plugkit's `ctx.tools` pipeline (so
  guards and approvers apply without the loop knowing), results fed back, and
  every decision written to the session log. Tool calls run bounded-parallel
  but are always logged in the order the model asked, so a replay is
  deterministic. Turns end with a stated reason — completed, blocked,
  max-steps, max-tokens, cancelled, or failed — on every path including an
  exception.
- **Pending input that survives a restart** (`Inbox`): messages delivered but
  not yet processed live in two queues whose every change is a session event,
  so `Inbox.replay(session)` rebuilds them exactly.
- **The system prompt as a registry** (`pydsh.prompt`, `ctx.system_prompt`):
  ordered sections a plugin contributes without seeing the whole, runtime
  contexts, strict `{{variable}}` interpolation that fails loudly rather than
  rendering a broken prompt, and a configurable tool order. Mounted, the loop
  builds its system prompt from it; unmounted, it falls back to
  `AgentOptions.system`.
- **Projections** (`ctx.session_projections`): a domain contributes three pure
  functions — `init`, `apply`, `view` — and the framework owns the
  subscription, the per-session watermark cache, and the change stream.
  Identity is the change gate, so a unit that ignores an event costs nothing on
  it. Includes the cold-read ladder (`checkpoint` / `view_checkpoint` /
  `restore`) a durable cache calls, version-gated so a stale row is dropped
  rather than fed forward.
- **Session stats** (`ctx.session_stats`): the first unit — turns, steps, model
  time, tool time, time-to-first-token, and decode rate, folded from the log.
- **Automatic durability** (`ctx.checkpoint_policy`): flushes every N turns, so
  a conversation reaches disk without a consumer remembering to ask.
- **Storage** (`pydsh.storage`): a hub (`ctx.storage`) that does no I/O and
  holds named backends, two interchangeable media (a JSON file per unit, or
  SQLite rows), and a domain form (`ctx.storage_domain`) that owns what a
  record *means* — declared domains, schema-validated tables, synchronous
  reads from memory, one write chain per domain, and a change event. Writes go
  durable first, memory second, event third, so the read path is never ahead of
  what is on disk.
- **The operating core** (`pydsh.operating`): `ctx.settings` (namespaced,
  validated, changeable while the process runs — the agent loop reads its
  parallel-tool limit through it), `ctx.credentials` (a name in config, the
  secret resolved per call and never returned by `describe`), `ctx.commands`
  (slash-commands that run without a model turn and never raise at the person
  who typed them), and `ctx.anonymous_user_id`.
- **The projection cache** (`ctx.projection_cache`): checkpoint rows persisted
  through the storage seam, so listing archived conversations with their stats
  is a table read rather than a hundred log folds. It may lag the log; it never
  leads it.
- **Capability seams** (`pydsh.capability`): `ctx.fs` (a file system whose
  execution root holds against symlinks, and whose reads are bounded in cost
  rather than only in output), `ctx.shell` (one-shot commands whose timeout
  signals the whole process group, so nothing is left running), `ctx.terminal`
  (shells that keep their state between calls), and deadlines that are
  distinguishable from cancellations.
- **Bounded output** (`pydsh.bounded`): retainers that bound a stream as it
  arrives (in items, or in bytes with UTF-8 cuts that never break a character),
  a spill store that puts oversized output where the model can read it back,
  and a deterministic pruner that cuts the middle out of an over-budget result.
  Everything that drops data says how much.
- **Cancellation** (`pydsh.cancel`): `AbortSignal` semantics, with two scopes
  per agent. `cancel()` stops the work in flight and leaves the agent usable;
  only a lifetime abort — the caller tearing down, or the loop being unmounted
  — ends it.

No provider adapter ships here — `openai_compatible`, `deepseek` and `pi_ai`
are plugins in a later sprint. `plan_mode` (the rest of the Agent seam, which
needs commands and projections first) and the wider service catalogue are
queued in the order
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
