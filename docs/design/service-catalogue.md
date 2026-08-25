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
Layer 0 (`context`, `fiber`, `reflect`, `registry`, `service`, `events`,
`logger`, `signal`, `loader`, `hmr`, `schema`) is **plugkit-shipped**, every seam
and support service is **core**, Layer 4 is **app-layer**, the provider
adapters are **provider-domain**, and application-shaped concepts are
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
| `brand` (MessageId/CallId, nominal brands) | **convention, not a module** | the reference's `brand.py` is a type alias plus a docstring with no runtime behaviour. The convention it documents — one `str` subclass per id type, constructed through its owner's factory — is the rule here; porting the empty module to claim the row would be paper coverage |

### LLM seam

| Service | Class | Status / note |
|---|---|---|
| `llm` (ctx.llm) | core | register_adapter / stream / prepareCall-ish surface |
| `call_config` (3-layer merge) | core | provider < header < request |
| `retry_policy` | core | modes + backoff + retryable codes |
| `attribution` (User-Agent) | core | must be sent by every HTTP adapter |
| `token_meter` (ctx.tokenMeter) | core | heuristic estimator |
| `brand` | core | covered above |
| adapters: `openai_compatible` | **provider-domain** | the default plugin. The block index is allocated by the translator, not taken from the wire's `tool_calls[].index` — that number is the provider's own tool numbering and collides with the text block at 0 |
| adapters: `deepseek` | **provider-domain** | extends `openai_compatible` rather than copying it; adds quota/context-overflow classification and `thinking` resolution |
| adapters: `pi_ai` | **provider-domain** | the catalogue-driven adapter: a built-in provider/model table with capacities, resolved against config once at mount and refusing anything this build cannot serve. Protocol and thinking-format tables are **narrower** than the reference's (one protocol, two formats) and say so on refusal, rather than accepting a config that fails at the first request |

### Agent seam

| Service | Class | Status / note |
|---|---|---|
| `agent` (ctx.agents) | core | registry + pluggable loop factory |
| `agent` Agent (1:1 session, insert/run/cancel/when_idle) | core | the loop driver |
| `inbox` (ctx-free per-agent) | core | two queues + splice events (replayable) |
| `plan_mode` | core | last-wins `plan/mode` fold, plus a pending intent held to the next turn boundary. The pending value is deliberately **not** rendered into the prompt section — the reference does, which applies a queued flip mid-turn while still reporting it as queued |
| `subagent` (tool plugin) | plugin | the tool ships here as a default plugin; the *mechanism* it uses (a child session + `create_agent`) is core and already was |
| `system_prompt` (ctx.systemPrompt) | core | sections/contexts/tools/variables + assemble waterfall |

### Tools seam

| Service | Class | Status / note |
|---|---|---|
| `tools` (ctx.tools) | **plugkit-shipped** | reuse `ToolsService`; map reference semantics onto it, don't re-port |
| `guard_repeat_tool` | core (a guard plugin) | listens `tools/post-execute` |
| `guard_timeout` | **plugkit-shipped** | plugkit's `timeout_policy` is the same stage-3 wrapper over each tool's own budget; re-porting it would be two implementations of one concern (lens S3) |
| the `tool_*` plugins | **plugin** | see the Default plugins table below — every one is a `ctx.tools.register` + handlers |

### Default plugins (the reference's whole plugin layer)

Parity is at the **service *and* default-behaviour level**: the services are the
seams, and these 17 plugins are the default behaviour that proves the seams
compose. They are the pieces a consumer swaps rather than writes. Every one is
mounted, none is privileged — replacing any is a matter of mounting another
plugin that provides the same contribution.

| Plugin | Class | Seam it exercises |
|---|---|---|
| `tool_bash` | plugin | `tools` + `shell` |
| `tool_fs` | plugin | `tools` + `fs` |
| `tool_terminal` | plugin | `tools` + `terminal` |
| `tool_todo` | plugin | `tools` |
| `tool_goal` | plugin | `tools` + `goal` |
| `tool_jobs` | plugin | `tools` + `jobs` |
| `subagent` | plugin | `agent` (child session + create_agent). Depth is carried on the calling agent, not counted in a shared integer — the reference's counter measures concurrency, so parallel siblings exhaust it |
| `guard_repeat_tool` | plugin | `tools/post-execute` |
| `guard_timeout` | plugin | `tools/execute` |
| `spill_policy` | plugin | `spill` + `tools/post-execute` |
| `long_term_memory` | plugin | `agent` `turn/end` recall |
| `hooks` | plugin | `hooks_protocol` |
| `time_context` | plugin | `agent/pre-step` context injection |
| `system_instructions` | plugin | `agent/pre-step` context injection |
| `command_goal` | plugin | `commands` + `goal` |
| `command_feedback` | plugin | `commands` + the session log. **Not** `message_feedback`: this records a log-only `feedback/record` event about the whole session, where `message_feedback` is a sidecar rating on one message |
| `command_compact` | plugin | `commands` + `compaction`, routing refusals by `CompactionRefused.code` |

The two `*_context`/`instructions` plugins matter more than their size suggests:
they are the reference's demonstration that context reaches the model as
*model-visible history* with a plugin `MessageSource`, not by mutating the system
prompt. A port that skips them loses the proof that the injection seam works.

### Storage / capability seams

| Service | Class | Status / note |
|---|---|---|
| `storage` (ctx.storage hub) | core | the deferral ended when the second store arrived, as planned. The session log keeps its bespoke SQLite table; everything since goes through the hub |
| `storage_domain` (ctx.storageDomain) | core | schema-validated KV domains + change events |
| `storage_json` / `storage_sqlite` | core | two interchangeable media, kept behind the hub |
| `storage_kv` | core, **covered by `storage_json`** | the reference's `KvTable` is a JSON file with an atomic replace and a detached in-memory copy — which is exactly `JsonBackend`/`JsonKvUnit` behind the hub. Porting it separately would be a second implementation of one medium |
| `fs` (web/read/edit) | **plugkit?** no → core | zero-dep path/text service; or map to reference `fs` seam |
| `atomic_write` (util) | core | tiny util |
| `spill` / `spill_local` (ctx.spillStore) | core | huge-tool-text retention + locator |
| `spill_policy` | core (plugin) | post-execute enrichment |
| `shell` | core | subprocess execution seam |
| `terminal` | core | persistent shell sessions |
| `mcp_client` (plugin) | core | MCP bridge plugin (tools registered as `mcp__server__tool`). Disposal goes through plugkit's returned disposer rather than the reference's reach into `ctx.tools._tools`; the child's environment scrub is the **base** rather than an overlay over a full copy, which is what makes it remove anything; and a failed sync restores the previous generation |

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
| `message_feedback` | core | durable per-message sidecar, fenced by session *lifetime* (id + creation time), not by id — a reused id must not surface a previous life's ratings |
| `long_term_memory` (plugin) | core (plugin) | cross-session recall at turn/end |
| `attachment` / `attachment_local` (ctx.attachments) | core | content-addressed attachments |
| `attachment_image` | core, **partial** | validation only: size and declared type. Decoding image formats belongs to a consumer's chosen library, not to a stdlib-only core |
| `typert` (ctx.typert) | core | declarative remote-call protocol. Ported as a runtime **scan**, not a generator: the reference's TypeScript code generator exists because TS cannot read its own decorators at runtime, and Python can |
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
| `sdk` (in-process) | app-layer | `Harness` / `HarnessSession` / `RunResult`. Named for what it is rather than after a vendor — this repo is a general core. The JSON-RPC form is queued behind it |
| `protocol` / `server` / `client` | app-layer | `pydsh.runtime`: newline-delimited JSON-RPC 2.0, a booted context behind it, and a client that spawns one. Inbound requests are dispatched as tasks — the reference awaits them inside the read loop, which deadlocks the moment a handler calls back — and the stdin hand-off goes through `call_soon_threadsafe`, because `asyncio.Queue` is not thread-safe |
| `websocket` / `gateway` / `bridge` | app-layer | queued behind the stdio runtime |
| `cli` | app-layer | the harness CLI |
| `env` / `profile` / `loader` / `home_paths` | app-layer | `pydsh.boot`: layered `.env` (bootstrap-prefixed names refused from a file), profiles as data resolved before anything mounts, and one home resolution. `watcher` is **not** ported — plugkit owns plugin lifecycle and a config watcher is a consumer's choice |
| `home_paths` / `launch_environment` | app-layer | install/locate helpers |

## What is consciously NOT ported into core

These are consumer-domain — a consuming application's own plugins — per the
generality rule. Excluding them is the point, not a gap:

- **Auth / identity / roles** (JWT, group-based permissions) — the reference has
  none either; it belongs in the consumer's plugin.
- **Domain record stores** (cases, conversation snapshots, workspace app
  directories, artifacts-as-blobs) — a consumer's domain, built *on* the general
  seams rather than inside them.
- **A role-gated safe-invoke** — the general `tools/*` pipeline is provided here;
  the roles half is a `guard`/`approver` the consumer writes.

### Reference modules with no counterpart here, and why

Every other module of the reference is ported. These five are not, each for a
stated reason rather than because nobody got to them:

| Module | Why not |
|---|---|
| `tools` | **plugkit-shipped.** `ToolsService` is the same five-stage pipeline; a second one would be two implementations of one concern |
| `guard_timeout` | **plugkit-shipped.** `timeout_policy` is the same stage-3 wrapper over each tool's own budget |
| `brand` | **a convention, not a module.** The reference's file is a type alias and a docstring with no runtime behaviour. The convention it documents — one `str` subclass per id type, built through its owner's factory — is the rule here; porting the empty module to claim the row would be paper coverage |
| `hooks` | **consumer-domain.** `hooks_protocol` (the dialect-neutral seam) *is* ported; the dialect plugins encode one product's conventions |
| `watcher` | **not a seam.** plugkit owns plugin lifecycle, and whether to reload on a config change is a consumer's policy, not a general capability |
| `native_command` | **an OS helper with no consumer here.** It exists for the reference's CLI integrations; nothing in this core calls it, and a helper with no caller is surface without a contract |
| `launch_environment` | **folded into `boot/envfile.py`**, which does the layering the reference split across two files |

## Coverage as of 2026-08-25

All 84 modules of `dsh-python` are accounted for: **77 ported**, **7 recorded
above**. The gate is `uv run pytest tests -q` — 1264 tests, including a real
child process speaking JSON-RPC, real subprocess kills by process group, and a
persistence round-trip across two processes.

## General rule (the how)

- **Core = the reference's general seams + the shared value vocabulary**
  (`message`, `brand`, session/agent/llm event vocabularies).
- **Provider-specific = plugin, swap via config** (`deepseek`, `pi_ai`, `openai_compatible`,
  `*_local` backends, the `*‑tool` plugins).
- **Consumer-specific = above pydsh entirely.** Consumers replace a plugin/service by
  mounting another, per the reference's "no privileged core" model.
- Transport stays at the adapter boundary (httpx at the LLM/MCP transport); the four seams
  stay stdlib-pure in core.
