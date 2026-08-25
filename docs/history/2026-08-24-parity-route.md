# The route to catalogue parity — 2026-08-24

The coverage contract is `docs/design/service-catalogue.md`; that document says
*what* is in scope and stays authoritative. This one is the *order* the sprints
run in, written down once so a session that picks the work up mid-route does
not have to re-derive it. It is a plan, so it lives here in history and is not
maintained as truth — the specs and the catalogue are.

## Where the line is

Ported and shipped: the session log with SQLite persistence (01), the message
vocabulary and the LLM seam (02), the agent loop (03).

The reference surface still to port is roughly 13k lines across
`services/`, `plugins/`, `util/` and `api/` in `reference/dsh-python/dsh_py/`.

## Order, and why

Each sprint is a bounded batch that leaves the tree green and installable. The
sequence is bottom-up: a sprint never depends on a later one.

| Sprint | Batch | Why here |
|---|---|---|
| 04 | `system_prompt`, `plan_mode` | finishes the Agent seam; the loop already has the fallback seam for it |
| 05 | `projection`, `projection_cache`, `session_stats`, `session_query`, `session_reference`, `CheckpointPolicy`, `brand` | the session seam's read side; everything above reads history through it |
| 06 | `storage` hub, `storage_json` / `storage_kv` / `storage_sqlite`, `storage_domain`, `atomic_write`, `fs` | the second store arrives, which is what the hub was deferred for |
| 07 | `settings`, `credentials`, `commands`, `anonymous_user_id`, `retention`, `timeout` | operating core; `settings` unblocks the loop's live parallel limit |
| 08 | `shell`, `terminal`, `spill`, `spill_local`, `tool_result_pruner` | execution + large-output seams the default tools need |
| 09 | `guard_repeat_tool`, `guard_timeout`, `tool_bash`, `tool_fs`, `tool_terminal`, `tool_todo`, `time_context`, `system_instructions`, `spill_policy` | the default behaviour that proves the seams compose |
| 10 | `jobs`, `jobs_local`, `schedule`, `schedule_domain`, `goal`, `goal_fold`, `tool_jobs`, `tool_goal` | background and durable-objective services |
| 11 | `hooks_protocol`, `hooks`, `message_feedback`, `long_term_memory`, `attachment*`, `typert`, `invariants` | the remaining operating services |
| 12 | `compaction`, `compaction_basic`, `command_compact`, `command_goal`, `command_feedback`, `subagent` | compaction needs `surfaceOp: replace`, deferred since spec 01 |
| 13 | `openai_compatible`, `deepseek`, `pi_ai`, `mcp_client` | provider-domain plugins; nothing before them may depend on a provider |
| 14 | `sdk`, `protocol`, `client`, `server`, `websocket`, `gateway`, `cli`, config/boot shells, `home_paths` | the app layer, thin over core |

## The standing rule for every sprint on this route

Read the reference module before porting it, and where the reference is wrong,
deviate deliberately and record it in that sprint's Decisions. Sprint 03 found
six such defects in one module; reproducing them for fidelity's sake would be
porting the bugs, not the semantics.

## Revision — 2026-08-25

The order below shifted as sprints landed, for reasons worth recording:

- 08 became the *capability* seams (`fs`, `shell`, `terminal`, `timeout`) and
  09 the *bounded-output* family (`retention`, `spill`, `tool_result_pruner`).
  They were one sprint on paper; they are two concerns.
- **Compaction moves up to 10.** It owns the `surfaceOp: replace` machinery
  that spec 01 defined and did not implement, and two things now wait on it:
  compaction itself and `prune_session`. A blocker for two consumers is not a
  late sprint.
- The default tool plugins follow compaction, since the guards among them
  (`guard_repeat_tool`, `spill_policy`) hook the tools pipeline and want the
  bounded-output family that now exists.
