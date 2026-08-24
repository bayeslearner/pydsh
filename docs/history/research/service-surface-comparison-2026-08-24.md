# Service-surface comparison — dshpy scoping

Date: 2026-08-24. Research note, not a decisions doc. Feeds the spec queue
(what to port, in what order, how generally).

Question answered: how far the Python port can go without losing the
generality the reference demands, and what prismi3 needs from it when its
backend is refactored onto this port.

## Sources

- Reference (TS spec): `agentic-harness-eval/docs/history/clones/deepseek-harness` —
  233 `package.json`s under `packages/`, READMEs + `docs/subsystems/*` + `docs/architecture.md`.
- Existing Python port to mine: `agentic-harness-eval/docs/history/clones/dsh-python`
  (havocio/dsh-python, MIT, 34.5k LOC, 98 non-test modules).
- prismi3 backend (target adoption):
  `~/Dropbox/Projects/work-prismi3-agent` `src/backend/`.

## Field survey: other Python ports

Only one serious Python port was found: **dsh-python** (`github.com/havocio/dsh-python`),
a one-to-one Chinese-commented re-implementation of the whole harness on a weak hand-rolled
Cordis kernel. It is the reference for *what services exist and their public surface*, not for
*how* — its kernel (`dsh_py/core/fiber.py`, 114 lines, no async unload, no FAILED state) is
explicitly not to be ported; plugkit replaces it.

dsh-python's coverage across the three seams (from its module map):

| Seam | Services |
|---|---|
| **Session** | `session`, `session_persistence`, `session_stats`, `projection`, `projection_cache`, `session_query`, `session_reference` |
| **LLM** | `llm`, `call_config`, `retry_policy`, `api_key`, `attribution`, `brand`, `token_meter`, adapters (`deepseek`, `openai_compatible`, `pi_ai`) |
| **Agent** | `agent`, `inbox`, `plan_mode`, `subagent`, `system_prompt`, `commands` |
| **Tools** | `tools`, `guard_repeat_tool`, `guard_timeout`, `tool_bash/tool_fs/tool_goal/tool_jobs/tool_terminal/tool_todo` |
| **Storage** | `storage`, `storage_domain`, `storage_json`, `storage_kv`, `storage_sqlite` |
| **Support** | `settings`, `credentials`, `spill*`, `compaction*`, `schedule`, `jobs*`, `goal*`, `hooks*`, `feedback`, `long_term_memory`, `retention`, `mcp_client`, `typert`, `watcher`, `time_context`, `home_paths`, `atomic_write`, `brand`, ... |

That map is a good "which services exist" catalogue and is already reflected in the
[reference package list](#reference-service-catalogue). The port's job is to reproduce the
*surface and semantics* of these seams on plugkit, not to copy dsh-python's code.

### Semantics worth copying verbatim (from the seam survey)

These are the recurring, hard-won contracts the port should preserve so downstream
`pi_ai`/`prismi3`-shaped consumers replace a plugin, not core:

- **Session**: every `append()` emits `session/event` (session, event) — the single bus all
  other seams (projection, stats, cache, checkpoint, long-term-memory) subscribe to.
  `SessionEvent{type, seq(1-based monotonic), time, data}`. `assistant/message` data is
  `{"turn","step","message","usage"}`; surface = `{nodes:[seqs], replace_generation}`;
  `derive_messages()` projects `user/message`→data, `assistant/message`→`data["message"]`,
  `tool/result`→`data["message"]`.
- **LLM**: `ctx.llm.register_adapter(providers, adapter)` all-or-nothing route commit +
  `handle.replace()` atomic re-route + `llm/adapters-updated`; `stream()` runs the
  `llm/stream` waterfall around the adapter; chunk types drive a `BlockAssembler`
  (`block-start/text-delta/…/usage/finish`). `call_config` is a 3-layer merge:
  provider defaults < session header (persisted) < per-request. `attribution` mandates a
  User-Agent on every provider request.
- **Agent/turn-step**: pre-step `agent/pre-step` waterfall → `enter|reject`; `agent/request-error`
  waterfall → `{"kind":"retry"}`; bounded `max_steps`/`max-tokens`; tool calls via
  `tools/execute` then `tools/post-execute` **waterfalls** whose listeners are the guards
  (repeat-tool, timeout) and enrichment (spill). Agent registry + pluggable loop factory
  (swap the loop = re-`set_factory`).
- **Inbox**: two queues (`next-turn`, `next-step`); every mutation writes an
  `agent/inbox/spliced` session event so the inbox is replayable across restarts.
- **Projection cache**: "a folding shortcut, never the authority" — a row may be stale (its
  `seq` says how old) but never wrong; write path fail-soft.
- **Dependency clamps**: the four seams are pure stdlib except the transport boundary
  (`httpx` in adapters/MCP client, optional `zstandard` for sqlite compression, `websockets`
  for a gateway), all behind lazy imports. The port should keep that split: stdlib core,
  transport at the adapter boundary.

## Key lever: plugkit already ships core seams

plugkit (the kernel) ships, as ordinary mountable services:
`ConfigService`, `PointsService`, `ReactiveService`, `SupervisorService`, `ToolsService`
(with a complete five-stage tool pipeline: `register/guard/set_approver/get/list/execute`,
`Allow/Deny/Ask/Accept/Block`, plus `timeout_policy`), `FileLoader`, and the full Cordis
event dispatch modes (`parallel`, `serial`, `emit`, `waterfall`, `bail`).

Consequence: the port does **not** re-port dsh-python's `tools.py` or a permission pipeline —
it maps the reference's `tools/*` seam onto plugkit's shipped `ToolsService`, and skips a
generic-KV storage layer (the session log is the only store, so a specific SQLite table wins).

## Reference service catalogue (dsh, 233 packages)

The reference's 58-service catalogue (the `plugkit` README's number) lives in dsh's own
`docs/architecture.md` and `docs/subsystems/*`. Core seams and their `ctx` keys:

| ctx key | Reference package | Purpose | Port needed? |
|---|---|---|---|
| `ctx.sessions` | `core/session` | event-sourced session log + store | yes, first (spec 01) |
| `ctx.llm` | `llm/llm` | provider-neutral model vocabulary + adapter seam (`registerAdapter`, `stream`, `prepareCall`) | yes |
| `ctx.agents` / `ctx.agentLoop` | `core/agent`, `core/agent-loop` | live agent registry + the default loop driver | yes (agent seam) |
| `ctx.tools` | `core/tools` | tool registry + guarded pipeline | **reuse plugkit's** `ToolsService` |
| `ctx.systemPrompt` | `core/system-prompt` | prompt-section + tool-schema assembly | yes |
| `ctx.goals` | `goal/goal` | same-session objective | yes |
| `ctx.jobs` | `jobs/jobs` + `jobs-local` | background work, `job_*` tools | yes |
| `ctx.commands` | `interaction/commands` | human commands, no model turn | yes |
| `ctx.fs` / `ctx.shell` / `ctx.terminals` / `ctx.sandbox` / `ctx.subprocess` | `fs/*`, `shell/*`, `terminal/*`, `sandbox/*`, `subprocess/*` | capability seams | seam today, local backends later |
| `ctx.settings` / `ctx.credentials` / `ctx.sessionTitle` / `ctx.sessionStats` | `settings/*`, `credentials/*`, `session-title/*`, `session-stats` | support | later |
| `plan-mode`, `spill*`, `compaction*`, `message-feedback`, `mcp-client`, `subagent*`, `typert`, `schedule`, `todo`, `hooks/*`, `identity`, `workspace` | ... | the 58-service long tail | later, as each lands |

the remaining ~170 packages are UI/client modules, presets, and per-provider plugin forks
(`llm-deepseek`, `llm-pi-ai`, `fs-local`, `sandbox-local`, ...) — the *implementations* a user
swaps in, not the general seams.

## prismi3's backend, and what dshpy must provide for it

From the backend survey (`src/backend/`):

### What prismi3 has today

- **Boot**: FastAPI app → `signalpy` kernel (DI + reactive, 3 rings) → components provide
  services via `@provides/@requires` → routes mounted by an `HTTPRoutes` component → agent
  runs a PocketFlow graph loop. `entry.py` is the boot sequence.
- **Auth**: JWT local + reverse-proxy; roles via `Identity.groups[]`; `workspace/auth/users.yaml`.
- **Storage**: every record is a file on disk + a single SQLite WAL `index.db` fast-query
  layer (`models.py`, WAL + `synchronous=NORMAL`). Three stores: `CaseStore`
  (YAML + content-hash optimistic lock), `ConversationStore` (JSONL messages + in-memory
  write locks — **no event log**), `ArtifactStore` (sha256 blobs + index).
- **Agent loop**: PocketFlow graph `compact → call_llm → decide → dispatch → (loop) →
  answer → structured`. No persistent inbox, no turn/step concept, no event-sourced history —
  per-request `shared` dict; conversation is message snapshots.
- **Tools**: `MCPServerManager` is the single merged authority (tool gateway + safe invoker +
  MCP bridge). Discovery of external MCP servers, aggregation of native `IToolPack`s, policy
  (tiers/enable/deny/destructive), `safe_invoke` envelope (Pydantic validation → role auth →
  timeout → size cap → structured error), plus `SystemMCPServer` re-exposing native packs as
  an MCP server.
- **Events**: in-memory `SSEEventBus` (per-subscriber queue, drops slow consumers, no replay).
- **Apps**: domain apps (`splunk`, `gitlab`, `google_workspace`, `cribl`, `kestra`, `ssh`,
  `nagios`, `servicenow`, `n8n`) each an `IToolPack`; `system_tools` is the agent's core pack.

### The gaps dshpy must cover for a prismi3 refactor

1. **Event sourcing.** prismi3's ConversationStore is message snapshots with no durable event
   log. The reference (and spec 01) makes the session log the append-only source of truth —
   this is the foundation that lets a later agent loop, tool log, compaction, and telemetry
   all be projections. This is the single biggest conceptual lift and the reason spec 01 leads.
2. **Turn/step + inbox + resume.** prismi3's loop has no turn/step model, no checkpoint, no
   resume across requests beyond re-reading stored messages. The reference's `turn/*`,
   `step/*`, pre-step/inbox, and durable-checkpoint (`session/flush`) semantics are the missing
   machinery. Depends on 1.
3. **The tool pipeline as a pluggable seam.** prismi3's `safe_invoke` envelope (validation →
   role auth → timeout → size cap) is close to the reference's `tools/*` permission pipeline;
   dshpy should expose it on plugkit's `ToolsService` so prismi3's policy/`interrupt` human-
   approval can be a `guard`/`approver` rather than a bespoke layer.
4. **Scoped/multi-tenant isolation.** prismi3 uses kernel "rings" + an `isolate` realm in the
   reference for grouping one agent's registrations. The reference's `core/scope` primitive
   (per-agent scoped registration) is the general mechanism.
5. **A single queryable store.** prismi3 already uses SQLite WAL for its index; dshpy's
   SQLite session log slots into the same file strategy, so the two combine without a new
   engine.

### What prismi3 has that the reference does not (don't generalize away)

The reference explicitly has no users, roles, or tenancy — dshpy must keep that boundary.
prismi3's auth (JWT/roles), CaseStore (case-kind envelope, optimistic locking), artifact store,
workspace app system (`_apps/<app>/case_sources|tags|skills`), and the safe-invoke role gate
are prismi3 *domain* concerns. They compose as prismi3's own plugin layers **above** the
reference's general seams (`ctx.sessions`, `ctx.llm`, `ctx.tools`), or as prismi3-owned
services — not as dshpy core. Otherwise dshpy stops being default/general.

## Generality rule for the port

- **dshpy core = the reference's general seams + `session`/`llm`/`agent`/`tools` vocabularies.**
  Provider-specific and domain-specific implementations live in plugins a consumer swaps.
- A consumer replaces a plugin/service the way the reference intends (edit config, mount
  another plugin), never by editing dshpy.
- Do not let prismi3-shaped concepts (users, roles, cases, workspace apps, safe-invoke role
  gate) leak into core. They are the *consumers'* plugins. The port stays general and default.

## Open questions

- How much of `core/scope` (realm/isolate) is needed before multi-agent/session isolation is
  real for prismi3? First-class soon, or deferred?
- Which provider adapters ship in core for the `ctx.llm` seam — an OpenAI-compatible one only
  (the default), with `deepseek`/`pi_ai` as plugins?
- Does prismi3 keep `signalpy` for its own domain components and mount dshpy's services inside
  it, or does dshpy's plugkit become the single kernel? (Affects the boot spec.)
