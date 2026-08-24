# Service Catalogue — the port's coverage contract

Canonical answer to "what does pydsh port?". The port covers **100% of the
dsh-python service surface** (and the reference seam it mirrors), except where
a piece is already provided by plugkit, or is a domain/consumer-specific layer
that does not belong in a general core.

This document is the single source of truth for coverage. Every spec that
ports a service anchors here. Reference direction: this design doc is pointed
*at* by specs; it points only at steering, never back at a spec id.

The dsh-python README §6 "复刻进度 (replication progress)" is the upstream
author's own coverage map: Layer 0 kernel / Layer 2 three seams (Session,
Agent, LLM) / Layer 3 support services / Layer 4 app layer / extra adapters,
all ✅. This catalogue is that map, re-generated against pydsh's kernel choice:
Layer 0 (fiber/scope/schema/loader/hmr) is **plugkit-shipped**, every seam
and support service is **core**, Layer 4 is **app-layer**, the provider
adapters are **provider-domain**, and prismi3-shaped concepts are
**consumer-domain**.

## Coverage classes

| Class | Meaning |
|---|---|
| **plugkit-shipped** | the kernel already provides it; pydsh reuses, does not re-port |
| **core** | ported into pydsh as a general seam/service |
| **app-layer** | harness-user-facing entry (CLI / gateway / SDK / config files) — ported, but thin shells over core; not core itself |
| **provider-domain** | a provider-specific or single-domain implementation — ported as a *plugin*, swap via config, not core |
| **consumer-domain** | belongs above pydsh entirely (a consumer's own plugins); at most an integration example |

## Coverage by dsh-python seam

### Session seam

| Service | Class | Status / note |
|---|---|---|
| `session` (ctx.sessions) | core | spec 01 — the event-sourced log + store |
| `session_persistence` (jsonl + sqlite) | core | spec 01 (sqlite first; jsonl backend kept, both are the same seam) |
| `session_header` (`request`/`call-config`, `seed_length`) | core | folded into `session` |
| `message` (content blocks, Message, MessageSource, payload encode) | core | the shared value vocabulary; every seam uses it |
| `session_stats` | core | folded projection (turns/steps/ttft/decode/toolMs pairing) |
| `projection` (ctx.sessionProjections) | core | the fold primitive |
| `projection_cache` (ctx.sessionProjectionCache) | core | "shortcut, never authority" |
| `session_query` / `session_reference` | core | history read/trace/filter + cross-session snapshot refs |
| `session_persistence.CheckpointPolicy` | core | turn-count checkpointing |
| `brand` (MessageId/CallId, nominal brands) | core | tiny typing helper |

### LLM seam

| Service | Class | Status / note |
|---|---|---|
| `llm` (ctx.llm) | core | register_adapter / stream / prepareCall-ish surface |
| `call_config` (3-layer merge) | core | provider < header < request |
| `retry_policy` | core | modes + backoff + retryable codes |
| `attribution` (User-Agent) | core | must be sent by every HTTP adapter |
| `token_meter` (ctx.tokenMeter) | core | heuristic estimator |
| `brand` | core | covered above |
| adapters: `openai_compatible` | **provider-domain** | ship as the default plugin |
| adapters: `deepseek`, `pi_ai` | **provider-domain** | ported as plugins, swap via config |

### Agent seam

| Service | Class | Status / note |
|---|---|---|
| `agent` (ctx.agents) | core | registry + pluggable loop factory |
| `agent` Agent (1:1 session, insert/run/cancel/when_idle) | core | the loop driver |
| `inbox` (ctx-free per-agent) | core | two queues + splice events (replayable) |
| `plan_mode` | core | last-wins `plan/mode` fold |
| `subagent` (tool plugin) | **provider-domain?** | subagent *tool* is a consumer-facing plugin; the *mechanism* (child session + create_agent) is core |
| `system_prompt` (ctx.systemPrompt) | core | sections/contexts/tools/variables + assemble waterfall |

### Tools seam

| Service | Class | Status / note |
|---|---|---|
| `tools` (ctx.tools) | **plugkit-shipped** | reuse `ToolsService`; map reference semantics onto it, don't re-port |
| `guard_repeat_tool` | core (a guard plugin) | listens `tools/post-execute` |
| `guard_timeout` | core (a guard plugin) | listens `tools/execute`; or plugkit `timeout_policy` |
| tool plugins: `tool_bash`, `tool_fs`, `tool_goal`, `tool_jobs`, `tool_terminal`, `tool_todo` | **provider/plugin** | ported as the reference's `*‑tool` plugins; each is a `ctx.tools.register` + handlers |

### Storage / capability seams

| Service | Class | Status / note |
|---|---|---|
| `storage` (ctx.storage hub) | defer | only deferred because spec 01 uses a specific SQLite table (the log is the only store today); the hub arrives when a second store appears |
| `storage_domain` (ctx.storageDomain) | core | schema-validated KV domains + change events |
| `storage_json` / `storage_kv` / `storage_sqlite` | core | the three backends, kept behind the hub |
| `fs` (web/read/edit) | **plugkit?** no → core | zero-dep path/text service; or map to reference `fs` seam |
| `atomic_write` (util) | core | tiny util |
| `spill` / `spill_local` (ctx.spillStore) | core | huge-tool-text retention + locator |
| `spill_policy` | core (plugin) | post-execute enrichment |
| `shell` | core | subprocess execution seam |
| `terminal` | core | persistent shell sessions |
| `mcp_client` (plugin) | core | MCP bridge plugin (tools registered as `mcp__server__tool`) |

### Operating / domain services

| Service | Class | Status / note |
|---|---|---|
| `settings` (ctx.settings) | core | namespaced, schema-validated runtime config |
| `credentials` (ctx.credentials) | core | `^[A-Za-z_]\w*$` refs, env/in-memory |
| `commands` (ctx.commands) | core | slash-command registry, no model turn |
| `jobs` / `jobs_local` (ctx.jobs) | core | background tasks, owner-session fencing |
| `schedule` / `schedule_domain` | core | durable reminders on the session log |
| `goal` / `goal_fold` (ctx.goals) | core | event-sourced persistent objective |
| `hooks_protocol` | core | dialect-neutral external-command hooks |
| `message_feedback` | **core?** | durable per-message sidecar; fence with persistent-session identity |
| `long_term_memory` (plugin) | core (plugin) | cross-session recall at turn/end |
| `attachment` / `attachment_local` / `attachment_image` (ctx.attachments) | core | content-addressed attachments |
| `typert` (ctx.typertRegistry) | core | declarative remote-call protocol |
| `tool_result_pruner` | core | replay-safe tool-result trimming |
| `compaction` / `compaction_basic` | core (later) | surface replace + summary; blocked by spec 01's `surfaceOp: replace` deferral |
| `invariants` | core (later) | runtime invariant registry |
| `anonymous_user_id` | core | home-scoped id |
| `retention` (util) | core | bounded-output library |
| `timeout` (util) | core | shared timeout primitive (plugkit may cover) |
| `native_command` (util) | app-layer | OS integration helper |

### App-layer (porterd as shells over core, not core)

| Module | Class | Note |
|---|---|---|
| `sdk` (in-process + jsonrpc) | app-layer | thin over core |
| `client` / `server` / `protocol` / `bridge` / `gateway` / `websocket` | app-layer | the JSON-RPC/WebSocket/gateway tier |
| `cli` | app-layer | the harness CLI |
| `dsh_config` / `env` / `profile` / `config` / `loader` / `watcher` | app-layer / plugkit | boot layers; plugkit's `load_app`/`FileLoader` covers load; keep the entry shells |
| `home_paths` / `launch_environment` | app-layer | install/locate helpers |

## What is consciously NOT ported into core

These are consumer-domain (prismi3's own plugins), per the generality rule —
excluding them is the point, not a gap:

- **Auth / identity / roles** (prismi3 JWT/roles) — reference has none; consumer's plugin.
- **CaseStore / conversation snapshots / workspace `_apps/` / artifacts-as-blobs**
  (prismi3 domain) — consumer's domain, built on the general seams.
- **`safe_invoke` role gate** — the general `tools/*` pipeline is provided; the prismi3
  *roles* part is a `guard`/`approver` the consumer writes.

## General rule (the how)

- **Core = the reference's general seams + the shared value vocabulary**
  (`message`, `brand`, session/agent/llm event vocabularies).
- **Provider-specific = plugin, swap via config** (`deepseek`, `pi_ai`, `openai_compatible`,
  `*_local` backends, the `*‑tool` plugins).
- **Consumer-specific = above pydsh entirely.** Consumers replace a plugin/service by
  mounting another, per the reference's "no privileged core" model.
- Transport stays at the adapter boundary (httpx at the LLM/MCP transport); the four seams
  stay stdlib-pure in core.
