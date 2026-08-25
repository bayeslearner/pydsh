# Parity reached — 2026-08-25

The standing order given on 2026-08-24 was "loop until you reach parity". This
records that it is met, and what "met" was measured against.

## The measure

`docs/design/service-catalogue.md` is the coverage contract. Every module of
`reference/dsh-python/dsh_py` is now either ported here or recorded *there* as
deliberately out of scope with the reason:

| | count |
|---|---|
| reference modules | 84 |
| ported | 77 |
| recorded as out of scope | 7 |
| unaccounted for | **0** |

The seven are `tools` and `guard_timeout` (plugkit ships both), `brand` (a
convention, not a module — the reference's file has no runtime behaviour),
`hooks` dialect plugins and `watcher` and `native_command` (a consumer's choice
rather than a general seam), and `launch_environment` (folded into
`boot/envfile.py`).

The gate is `uv run pytest tests -q`: **1264 tests**, including a persistence
round-trip across two processes, real subprocess kills by process group, a real
child speaking JSON-RPC, and the `pydsh` console script run for real.

## The sprints

03 agent loop · 04 system prompt · 05 session projections · 06 storage seam ·
07 operating core · 08 capability seams · 09 bounded output · 10 compaction ·
11 default tools · 12 jobs and goals · 13 session query · 14 schedule and hooks ·
15 sidecars and memory · 16 plan mode and commands · 17 OpenAI-compatible
adapters · 18 the catalogue adapter · 19 MCP · 20 boot and the SDK ·
21 the JSON-RPC runtime · 22 the gateway and the CLI.

All CLOSED / SHIPPED, in order, each with the suite green.

## What the method actually bought

The rule was *read the reference module before porting it, and where it is
wrong, deviate deliberately and record why*. Every sprint from 05 onward found
at least one defect. The ones worth remembering:

- **Surface replacement matched by sequence range**, so compaction worked
  exactly once per session and silently did nothing thereafter (10).
- **A waterfall guard read its decision off the wrong argument**, so the spill
  policy never fired — the code looked right and the value was always `None` (11).
- **`flush` recorded the session's current tail as its watermark** rather than
  what it wrote, so events appended during a flush were marked persisted and
  never written. Shipped in spec 01; found in 07, when the projection cache
  first drove two seams together (07).
- **`killing a job` cancelled the task, not the process**, so the suite *hung*
  rather than failed and every test passed individually (12).
- **The MCP child-environment scrub removed nothing** — it copied the whole
  environment and then updated it with the scrubbed subset. A security control
  that does nothing is worse than none, because it stops anyone looking
  further (19).
- **The JSON-RPC read loop awaited its own handlers**, which is a deadlock
  rather than a slowdown the moment a handler calls back (21).
- **The wire's tool-call index was used as the harness block index**, so any
  response with text *and* a tool call produced two blocks claiming index 0 —
  the ordinary case, not an exotic one (17).

And one found in *this* port rather than the reference's, by a property test
written before the feature it tested: the gateway gave every client every other
client's conversation, because each connection's server subscribed to the
shared context's event feed (22).

The recurring lesson, recorded across several sprint logs: **building the
consumer, not just the seam, is what finds the defect.** The projection ladder
and the storage seam each passed their own sprint's tests; the data-loss bug
only appeared when a later sprint drove them together.

## What is not here, and is not a gap

No consumer is named anywhere in this repo, by design. Auth, roles, domain
record stores and workspace apps are a consuming application's own plugins —
see the catalogue's "consciously NOT ported" section.
