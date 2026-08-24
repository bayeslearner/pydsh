# Pillars — how pydsh (Python) succeeds

A Python port of DeepSeek Harness's service layer, built on the plugkit
kernel. It is a drop-in backend for any consumer that needs the seams rather
than a framework.

| Pillar | Current state | Healthy when |
|---|---|---|
| **Port / spec fidelity** | Session seam and LLM seam ported from the reference, every deliberate deviation recorded in the porting sprint's Decisions rather than left implicit. | Each service matches the TypeScript reference's semantics (event names, dispatch modes, lifetime rules); `test_conformance.py` asserts against the TS source, as plugkit does. |
| **Ship / MVP** | Reached and passed. A session survives a restart, and a model call streams end to end through `ctx.llm` with middleware able to wrap it. | A session survives a restart: events appended, persisted to SQLite, replayed, and `derive_messages()` reconstructs the same model history. |
| **Test / Examples** | 130 tests green. Honest where it matters: the persistence proof spans two real processes, and the retry guard asserts the *absence* of duplicated output rather than a happy path. | Conformance tests pass against the reference semantics; an end-to-end create→append→flush→reload→derive example runs clean. |
| **Design / Arch** | `data-architecture.md` complete for the storage tier; `service-catalogue.md` verified module-by-module against the reference, with no uncovered module. | Data-lifecycle table complete; every store has a writer, read path, and reproducibility guarantee. |
| **Packaging** | Installable from a git tag; the kernel is pinned so a consumer gets it automatically. Verified by installing into a clean environment, not by trusting a local checkout. | A consumer adds one dependency and gets a working kernel with it — no local path, no manual kernel clone. |
| **Documentation** | README states what actually works and what deliberately does not; the reference checkouts and the porting method are written down. | README + dev guide; the port's service catalogue mapped to the TS reference. |
| **Adoption** | Not started — no consumer has migrated. | A consumer runs on these seams instead of its own hand-rolled backend, keeping only its domain above them. |

Standing targets (retirement of consumers' backends is owned by those repos' own
sprints, not here).

**Packaging is a pillar, not a chore.** It was added after a release shipped
that installed cleanly from a local path and failed outright from its own tag —
the local install masked the defect. A release is healthy only once it has been
installed the way a consumer installs it.
