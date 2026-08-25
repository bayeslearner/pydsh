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

# Optional extras, both lazily imported:
#   [http] — the built-in HTTP transport for the provider adapters (httpx)
#   [ws]   — the WebSocket binding for the gateway (websockets)
uv add "pydsh[http,ws] @ git+https://github.com/bayeslearner/pydsh@v0.2.2"
```

It also installs a `pydsh` command:

```bash
pydsh --profile my_profile.py chat --provider openai --model gpt-4o "what changed?"
pydsh --profile my_profile.py gateway --port 8080      # needs [ws]
```

```python
from pydsh import AgentOptions, Harness, OpenAICompatible, core_profile

profile = [*core_profile(), (OpenAICompatible, {})]

async with Harness(profile, options=AgentOptions(provider="openai", model="gpt-4o")) as h:
    result = await h.session("my-chat").run("what changed today?")
    print(result.final_response)
```

`Harness` assembles a context from a profile, runs turns on named sessions, and
unmounts everything when its scope ends — including when the turn raised. The
answer comes back read out of the session log, so it is the same string a reader
of the transcript sees.

Nothing stops you mounting the seams yourself; the harness is the short path,
not the only one:

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

Mount a provider adapter: `OpenAICompatible` covers seven vendors and
`DeepSeek` its own, or write your own onto `ctx.llm`. They are plugins, not
core — see the coverage contract. For the built-in HTTP transport, install
`pydsh[http]`; supply your own `transport=` and you need nothing extra.

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
- **Compaction** (`pydsh.compaction`, `ctx.compaction`): replacing a stretch of
  history with a summary of it. The log stays append-only — one new event
  declares it shadows a range of the *surface*, and every original stays
  readable at its original sequence. A region may only be replaced if both its
  edges are balanced cuts, so a tool call is never separated from its result.
- **The default tools** (`pydsh.tools`): `read`/`write`/`edit` over `ctx.fs`,
  `bash` over `ctx.shell`, `terminal` over persistent sessions, `todo_write`,
  a repeat-call guard that reminds rather than refuses, a spill policy, and the
  two context injectors that demonstrate context reaching the model as history
  rather than as rewritten prompt. These are the behaviour that proves the
  seams compose, and the piece a consumer swaps rather than writes.
- **Jobs and goals** (`pydsh.work`): `ctx.jobs` runs background work owned by
  the session that started it — the owner is a fence checked on every
  operation, and a job you do not own is reported as absent rather than
  forbidden. `ctx.goals` folds a durable objective out of the session log with
  compare-and-set semantics, so two writers cannot silently overwrite each
  other; arming is process-local and never persisted.
- **Reading history** (`pydsh.query`): `ctx.session_query` searches the corpus
  — list, read, and filter sessions and their events by time, type, text, or
  where they sit on the surface. Filters are data, so a client can send them
  over a wire, and text search is literal. `ctx.session_references` gives
  canonical URIs and Markdown mentions for pointing one session at another,
  with a bounded projection so a reference is not a paste.
- **Schedules, hooks and invariants** (`pydsh.governance`): `ctx.schedules`
  holds durable reminders folded from the session log, with the timer as a
  projection that re-checks the log on every wake-up so nothing fires early;
  `ctx.hooks` runs a deployment's own commands at points in the loop and merges
  their answers conservatively — any block wins; `ctx.invariants` makes a
  violated assumption loud rather than mysterious.
- **Sidecars** (`pydsh.sidecar`): the things that are *about* a conversation
  without being *in* it. `ctx.attachments` stores immutable content under a
  content address — a reference names the bytes, never a place to look, and
  reading re-hashes so a swapped file is caught rather than served.
  `ctx.message_feedback` keeps a rating or note beside the log rather than on
  it, fenced by session lifetime and written compare-and-set.  `ctx.typert`
  exposes methods a class explicitly marks, so a public helper never quietly
  widens the remote API, and an invocation returns a structured failure instead
  of raising across a transport. `ctx.long_term_memory` captures each exchange
  keyed by content and recalls what overlaps into a later session — as history,
  like every other context here.
- **Plan mode** (`pydsh.plan`, `ctx.plan_mode`): a recorded collaboration
  state, folded from the log so a resumed session has it with nothing to
  rebuild. A flip requested while a turn is running is *held* until the next
  turn boundary — a turn runs under one set of rules from its first step to its
  last — and `exit_plan_mode` puts the finished plan to a review channel, or
  says plainly that there is none rather than inventing an approval.
- **Subagents** (`ctx.tools`' `subagent`): a child agent on a standalone
  prompt, in its own session, seeing nothing of the parent conversation. Depth
  is counted along the call chain, so five siblings started in parallel are all
  at the same depth; and the child is fused to the parent's *turn*, so
  cancelling the turn stops the child rather than leaving it to finish work
  nobody is waiting for.
- **The console** (`pydsh.console`): the commands a person types. `/compact`
  turns each compaction outcome into a sentence routed by code rather than by
  message text; `/goal` reads the current objective and never shows anyone a
  revision number; `/feedback` records what someone thinks of a conversation as
  a log-only event, so it never becomes part of the conversation it is about.
  None of them raises at the person who typed it.
- **Provider adapters** (`pydsh.llm.adapters`): the only part of pydsh that
  speaks to a network, and mounted as plugins rather than built in.
  `OpenAICompatible` covers the `/chat/completions` dialect and the seven
  vendors that speak it, registered dormant until their credential resolves;
  `DeepSeek` adds reasoning and a failure vocabulary that tells a spent quota
  apart from a rate limit, and a context overflow apart from a bad request —
  because those pairs want opposite responses. The transport is a seam, so a
  deployment can hand over its own HTTP client and every test here drives real
  SSE bytes without opening a socket. The default transport is httpx, an
  optional extra (`pydsh[http]`), imported lazily.
- **The catalogue adapter** (`ctx.pi_ai`): one plugin over many routes, each
  resolved against a built-in catalogue of *models* — what each holds, what it
  can produce, which modalities it takes, how it spells reasoning. Compaction
  budgets against a real context window instead of a guess, and a request for
  reasoning on a model that cannot reason is refused here rather than by the
  endpoint. Config lays over the catalogue field by field and anything this
  build cannot serve is refused **at mount**, with the supported set named — a
  narrow table that fails loudly beats a broad one that fails at the first
  request in production.
- **MCP** (`pydsh.mcp`, `ctx.mcp`): another process's tools, on our pipeline.
  A server's tools are registered on `ctx.tools` under `mcp__<server>__<tool>`,
  so guards, approvers and the spill policy apply without any of them knowing
  where the tool runs — the model sees one flat list. Names are derived so two
  distinct `(server, tool)` pairs can never collapse into one, a sync is all or
  nothing (and a failed one restores the previous generation rather than
  leaving the model with none of that server's tools), and each connection is
  supervised: bounded backoff, then giving up and taking the tools away rather
  than offering what cannot run.
- **The front door** (`pydsh.boot`, `Harness`): assemble, run, tear down.
  Under it: one answer to where a deployment keeps things (`~/.pydsh`, resolved
  in one place so two services cannot disagree); a layered environment where an
  inherited value always wins and a `.env` may *not* set a variable that decided
  how the process boots; and profiles as **data**, fully resolved before
  anything mounts, so a typo in entry nine cannot leave eight plugins running.
- **The runtime** (`pydsh.runtime`): the same SDK surface, one process away.
  Newline-delimited JSON-RPC 2.0 both ways — `RuntimeClient` spawns
  `python -m pydsh.runtime` (or takes a transport of its own), and session
  events arrive as notifications *while* the turn runs, so a caller can stream
  a conversation it is not hosting. Inbound requests are served concurrently,
  which is what lets a handler call back into its peer instead of deadlocking
  against the loop that would deliver the answer.
- **The gateway and the CLI** (`pydsh.gateway`, `pydsh`): the runtime over a
  socket, and a command in someone's hands. Each client gets its *own*
  `RuntimeServer` and its own event subscription, which is what stops two
  clients seeing each other's conversations. `pydsh chat`, `pydsh sessions`,
  `pydsh runtime`, `pydsh gateway` — and an expected failure prints one line,
  not a stack.
- **Cancellation** (`pydsh.cancel`): `AbortSignal` semantics, with two scopes
  per agent. `cancel()` stops the work in flight and leaves the agent usable;
  only a lifetime abort — the caller tearing down, or the loop being unmounted
  — ends it.

**The port is complete.** All 84 modules of the reference are accounted for in
[`docs/design/service-catalogue.md`](docs/design/service-catalogue.md): 77
ported, 7 recorded there as deliberately out of scope with the reason — two
that plugkit already ships, one that is a convention rather than a module, and
four that are a consumer's choice rather than a general seam.

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
