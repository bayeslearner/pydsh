# Data Architecture

Conforms to the coverage contract in [`service-catalogue.md`](service-catalogue.md) —
which services dshpy ports, which plugkit already ships, and which stay
consumer-domain. This doc covers the storage tier's lifecycle only.

The horizontal contract every state-touching spec anchors to. This document is
canonical; a sprint's `design.md` cites it and never re-states it.

## Tier model

Three tiers. The **business** tier is out of scope for dsh (the agent loop and
model calls live there, composed as plugins above this layer). dsh builds the
**storage** and **computation** tiers and the contract between them.

| Tier | Owns | Lives in |
|---|---|---|
| **Storage** | the durable session log on disk (SQLite) | `src/dshpy/session/` |
| **Computation** | the in-memory ordered surface (`derive_messages`), projection, invariant checks | `src/dshpy/session/` |
| **Business** | the agent loop, tools, prompt assembly, model calls | *not this repo* |

Boundary contract: **storage ↔ computation is the append-only event log.** The
computation tier reads the log; it never writes it. `Session.append(type, data)`
is the single writer entry point. "Model-visible means logged": anything that
reaches a model request must be reconstructable from the log.

## Data-lifecycle table

| Store | Writer | Immutable/rolling | Source-of-truth or derived | Read path | Retention | Reproducible? |
|---|---|---|---|---|---|---|
| SQLite session log | `Session.append` (via `SessionStore` / persistence backend) | immutable, append-only | **source of truth** | load log → rebuild in-memory `Session` | whole log for a session id | yes — load + replay reproduces the identical event list |
| In-memory session list | `SessionStore` (create/enter) | rolling | derived (from SQLite after restart) | `ctx.sessions.get/list` | current process only | no — rebuilt from SQLite on boot |
| In-memory ordered surface | `Session._apply_surface_replace` | rolling | **derived** from the log's surface events | `derive_messages()` | current process only | yes — recomputable from the log at any time; the recompute is faithful because replacement nodes carry `sourceEventSeqs` |

The last column is the one that catches recompute bugs: `derive_messages`
must never read storage state the log cannot reproduce. The surface is
incrementally maintained in memory and rebuilt from scratch on load — never
persisted as a separate source of truth.

## Durability contract (reference) 

The reference fixes the durability checkpoint on two events, and the port
adopts them verbatim (they are plugkit event-dispatch modes):

- **`session/event`** — emit (fire-and-forget append feed). Post-commit; the
  listener snapshot resolves before the log push; observer failures are logged
  and contained without failing the committed append.
- **`session/flush`** — **parallel** (awaited durability checkpoint). Every
  listener runs and the caller awaits all of them. The SQLite persistence
  backend subscribes here so a caller awaits the disk write.

Consequence for design: persistence is a *listener* on `session/flush`, not a
direct call out of `SessionStore.flush()`; `flush` fans out and awaits the
backend.

## Storage-engine choice

**SQLite** (`sqlite3` stdlib, WAL mode, one row per event) is the engine, per
the owner's direction. Rationale over alternatives:

- **JSONL files** (the TS default and dsh-python's `JsonlSessionPersistence`)
  win on minimalism but lose on the port's real requirement: a single queryable
  store. SQLite gives indexing, atomic appends, and transactional flush that a
  directory of `.jsonl` files lacks, at one stdlib import and no schema beyond
  the log itself.
- **A general KV backend** (`storage/storage-sqlite` in dsh-python) is
  over-abstraction here: the session log is the only store this sprint builds,
  so a generic key→JSON table buys nothing. The ladder stops at the specific
  table. `ponytail:` if a second store appears later, generalize then.

### Session-log table

```
sessions( id TEXT PRIMARY KEY, version INT, created_at INT, cwd TEXT, meta TEXT /* JSON */ )
events(   session_id TEXT, seq INT, type TEXT, time INT, data TEXT /* lossless JSON */,
          PRIMARY KEY (session_id, seq),
          FOREIGN KEY(session_id) REFERENCES sessions(id) )
```

`session_id, seq` is the natural key. `data` stores lossless JSON (see the
JSON rules in the session module). Appends are one transaction per event.

## Schema evolution

`SESSION_FORMAT_VERSION = 0`. The version is stamped into each `sessions`
header row and the persistence backend refuses any other version on load — no
migration while unreleased (dsh is pre-release; incompatible logs are rejected,
per the reference's rule). Bump exactly when an older runtime could not read a
new log with full semantic correctness.
