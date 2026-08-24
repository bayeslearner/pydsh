# CLAUDE.md — how to work on this project

This repo is a Python port of the DeepSeek Harness service layer, built on the
plugkit kernel. Long-term adoption targets: prismi3-agent and SAW.

## Starting a session

1. Read this file (process rules)
2. Load the `spec-driven-dev` and `work-discipline` skills
3. Open the ACTIVE spec at the head of `specs/` (start with the lowest-numbered
   non-CLOSED one)
4. Check `git log --oneline -10` for recent work

## Layout — where each kind of truth lives

- `specs/NN-name/` — sprint specs (requirements / design / tasks). The x-axis.
- `docs/steering/pillars.md` — product health across dimensions. The y-axis.
- `docs/design/` — stabilized cross-cutting design + anchor docs (e.g.
  `data-architecture.md`), which state-touching specs anchor to.
  `service-catalogue.md` is the **coverage contract**: the full list of what
  dshpy ports (all of the dsh-python surface except plugkit-shipped and
  consumer-domain), and each service's coverage class. New specs that port a
  service anchor to it.
- `docs/guides/` — audience-facing guides.
- `docs/history/` — dated review sets and time-bound artifacts (each
  `/spec-review` writes a fresh `docs/history/<date>-review/`).
- `src/dshpy/` — the package under development.
- `tests/` — `pytest`; the persistence round-trip test is the MVP proof.

## Commands

```bash
uv sync          # install deps + the local plugkit kernel dependency
uv run pytest tests -q    # run the whole suite
uv run pytest tests/test_persistence.py -q   # the MVP round-trip proof
```

Use `uv` — never pip, poetry, or bare `python`.

## Kernel dependency

plugkit ships as an installable package in
`~/Dropbox/Projects/bayeslearner-microkernel` (remote
`github.com/bayeslearner/plugkit`), wired here as a path dependency
(`[tool.uv.sources] plugkit`). It is NOT vendored into this tree. Its README
documents the kernel semantics that the TS reference (dsh's documentation)
also describes.

## Spec-driven discipline (hard rules)

- One sprint ACTIVE at a time, worked in numeric order. Start-from-recent is
  NOT the rule here — this is a young repo with a small queue, so follow the
  spec order (rule: never activate over an un-CLOSED lower spec).
- Every piece of work traces to a spec task; a code-only commit is a bug.
  Update the feature/status marks in the same commit as the work.
- Close a sprint in its entirety (`status: CLOSED` + `closed_as: SHIPPED`),
  marking every leftover `[-] DROPPED:` or `[>] → <spec_id>` — never leave it
  dangling.
- Commit and push diligently (standing authorization). `main` is fine.
- Verify honestly: the persistence round-trip test is real verification, not a
  coverage score. Tests must pass before reporting done.

## Engineering principles

- **Write-time self-check (Q1–Q7)** before every commit — correctness,
  concurrency, security, performance, resilience, design fitness, readability.
  The reference is `work-discipline` Part 2.
- **EP1 config-as-code**: no hardcoded model ids / magic tunables / ad-hoc
  `os.environ`. Paths like the kernel checkout and test DB live in config
  (`pyproject.toml` / fixtures), not scattered in code.
- **Simplest faithful port**: match the reference's semantics; do not
  over-abstract. When a generic seam (e.g. a general KV store) would buy
  nothing, prefer the specific table and mark the ceiling with a `ponytail:`
  comment.

## Verification

The agent-browser verification rule from the consumers' repos does not apply
here (no browser UI). Verification is the pytest suite, with the
persistence round-trip as its centerpiece.
