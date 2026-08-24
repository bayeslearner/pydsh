# Service-surface comparison — pydsh scoping

Date: 2026-08-24. Research note, not a decisions doc. Feeds the spec queue
(what to port, in what order, how generally).

Question answered: how far the Python port can go without losing the
generality the reference demands, and what a consuming application needs from
it when its backend is refactored onto this port.

## Sources

- Reference (TS spec): `reference/deepseek-harness` — 233 `package.json`s under
  `packages/`, READMEs + `docs/subsystems/*` + `docs/architecture.md`.
- Existing Python port to mine: `reference/dsh-python` (havocio/dsh-python, MIT,
  34.5k LOC, 98 non-test modules).
- Both are git-ignored clones; see `CLAUDE.md` for how to obtain them.

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

These are the recurring, hard-won contracts the port should preserve so that a
consuming application replaces a plugin, not core:

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

## Consumer-specific analysis — removed

This note originally carried a section analysing one consuming application's
backend and the gaps a refactor onto pydsh would have to close. That analysis
belongs in the consumer's own repo, not in the reference it depends on, and it
has been removed from here. What survives below is the part that is about the
port itself: the generality rule the analysis produced.

## Generality rule for the port

- **pydsh core = the reference's general seams + `session`/`llm`/`agent`/`tools` vocabularies.**
  Provider-specific and domain-specific implementations live in plugins a consumer swaps.
- A consumer replaces a plugin/service the way the reference intends (edit config, mount
  another plugin), never by editing pydsh.
- Do not let application-shaped concepts (users, roles, domain records, workspace apps, a
  role-gated safe-invoke) leak into core. They are the *consumers'* plugins. The port stays
  general and default.

## Open questions

- How much of `core/scope` (realm/isolate) is needed before multi-agent/session isolation is
  real for a consumer? First-class soon, or deferred?
- Which provider adapters ship in core for the `ctx.llm` seam — an OpenAI-compatible one only
  (the default), with `deepseek`/`pi_ai` as plugins?
- Does a consumer already running its own kernel mount pydsh's services inside it, or does
  plugkit become the single kernel? (Affects the boot spec.) — a question for that consumer's
  repo to answer, recorded here only because it shapes the boot seam.
