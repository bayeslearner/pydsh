# Pillars — how pydsh (Python) succeeds

A Python port of DeepSeek Harness's service layer, built on the plugkit
kernel. It is a drop-in backend for any consumer that needs the seams rather
than a framework.

| Pillar | Current state | Healthy when |
|---|---|---|
| **Port / spec fidelity** | Sprints 01–16 ported: the session log and its projections, the LLM seam, the agent loop, the system prompt, storage, the operating core, capability seams, bounded output, compaction, the default tools, jobs and goals, session query, governance, the sidecars, and plan mode with the console commands. Where the Python reference is wrong the port deviates deliberately and records why in that sprint's Log — every sprint since 05 has found at least one such defect, several of which would have shipped silently (surface replacement that worked exactly once per session; a waterfall guard reading its decision off the wrong argument, so the policy never fired). | Each service matches the TypeScript reference's semantics (event names, dispatch modes, lifetime rules); `test_conformance.py` asserts against the TS source, as plugkit does. |
| **Ship / MVP** | Reached and passed many times over. A session survives a restart, a model call streams end to end through `ctx.llm`, an agent drives a whole conversation with every decision in the log, and the durable services around it — storage, goals, schedules, memory — all sit on one seam. | A session survives a restart: events appended, persisted to SQLite, replayed, and `derive_messages()` reconstructs the same model history. |
| **Test / Examples** | 918 tests green. Honest where it matters: the persistence proof spans two real processes, the shell tests kill real process groups, the retry guard asserts the *absence* of duplicated output, and the loop's tool tests assert ordering under *inverted* latency rather than a happy path. The tests earn their keep — a hung suite (not a failed one) is what revealed that killing a job cancelled the task and left the process running. | Conformance tests pass against the reference semantics; an end-to-end create→append→flush→reload→derive example runs clean. |
| **Design / Arch** | `data-architecture.md` covers every store the port owns, including the three sidecar stores that are honestly *not* derived; `service-catalogue.md` verified module-by-module against the reference, with no uncovered module. | Data-lifecycle table complete; every store has a writer, read path, and reproducibility guarantee. |
| **Packaging** | Installable from a git tag; the kernel is pinned so a consumer gets it automatically. Verified by installing into a clean environment, not by trusting a local checkout. | A consumer adds one dependency and gets a working kernel with it — no local path, no manual kernel clone. |
| **Documentation** | README states what actually works and what deliberately does not, one entry per shipped sprint; the reference checkouts and the porting method are written down. The dev guide is still missing. | README + dev guide; the port's service catalogue mapped to the TS reference. |
| **Adoption** | Not started — no consumer has migrated. | A consumer runs on these seams instead of its own hand-rolled backend, keeping only its domain above them. |

Standing targets (retirement of consumers' backends is owned by those repos' own
sprints, not here).

**Packaging is a pillar, not a chore.** It was added after a release shipped
that installed cleanly from a local path and failed outright from its own tag —
the local install masked the defect. A release is healthy only once it has been
installed the way a consumer installs it.

**What building the consumer, not just the seam, keeps finding.** The projection ladder and the storage seam each passed their own sprint's tests; the data-loss bug in `flush`'s watermark only appeared once the projection cache drove them together, two sprints later. A seam with a green suite and no consumer is an untested seam — which is the argument for porting the default plugins rather than stopping at the interfaces.
