# Pillars — how pydsh (Python) succeeds

A Python port of DeepSeek Harness's service layer, built on the plugkit
kernel. It is a drop-in backend for prismi3-agent and SAW.

| Pillar | Current state | Healthy when |
|---|---|---|
| **Port / spec fidelity** | Empty repo, no code | Each service matches the TypeScript reference's semantics (event names, dispatch modes, lifetime rules); `test_conformance.py` asserts against the TS source, as plugkit does. |
| **Ship / MVP** | None | A session survives a restart: events appended, persisted to SQLite, replayed, and `derive_messages()` reconstructs the same model history. |
| **Test / Examples** | None | Conformance tests pass against the reference semantics; an end-to-end create→append→flush→reload→derive example runs clean. |
| **Design / Arch** | `data-architecture.md` stubbed | Data-lifecycle table complete; every store has a writer, read path, and reproducibility guarantee. |
| **Documentation** | None | README + dev guide; the port's service catalogue mapped to the TS reference. |
| **Adoption** | None | prismi3-agent and SAW consume this backend instead of their own. |

Standing targets (retirement of consumers' backends is owned by those repos' own
sprints, not here).
