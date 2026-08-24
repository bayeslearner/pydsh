# Continuation handoff — 2026-08-24

Written before the machine move. The next session should read this first, then
`CLAUDE.md`, then the head of `specs/`.

## One machine-specific dependency — the plugkit kernel path

`pyproject.toml` pins the kernel as a **path dependency**:

```toml
[tool.uv.sources]
plugkit = { path = "../bayeslearner-microkernel" }
```

That path is relative to this repo (`~/Dropbox/Projects/bayeslearner-dsh`), so
the next machine needs the kernel checkout **one directory up** at
`~/Dropbox/Projects/bayeslearner-microkernel` (remote
`github.com/bayeslearner/plugkit`). `ponytail:` this is a hardcoded relative
path until the kernel gets a tagged release, then it becomes a version pin.

On the new machine, after cloning:
```bash
git clone git@github.com:bayeslearner/plugkit ../bayeslearner-microkernel   # relative to this repo
uv sync && uv run pytest tests -q
```
`uv sync` must resolve the path — if the kernel is missing, that line fails
immediately and loudly.

## State at handoff

- **Repo:** `bayeslearner/pydsh` (remote `origin`), branch `main`.
- **Spec 01 (session log + SQLite persistence): CLOSED / SHIPPED.** All 18
  tests green. The `ssh`/pytest gate: `uv run pytest tests -q`.
- **Nothing in flight.** HEAD is `ce467cc` (close of spec 01).

## What the port project is

- A Python port of the DeepSeek Harness service layer on plugkit; adoption
  targets are prismi3-agent and SAW.
- The **coverage contract** is `docs/design/service-catalogue.md`: port 100% of
  the dsh-python surface except plugkit-shipped (tool registry, event dispatch)
  and consumer-domain (auth/roles, cases, workshop apps). Build order is
  defaults-first; nothing provider-specific or consumer-specific ships before
  the general seams.
- Research/context: `docs/history/research/service-surface-comparison-2026-08-24.md`.

## Suggested next sprint

**Spec 02 — the LLM seam** (`ctx.llm` + the message/stream vocabulary +
`call_config` 3-layer merge + retry policy), which the agent loop depends on.
See `service-catalogue.md` "LLM seam" rows. The `message` value vocabulary is
a dependency of the agent loop and worth scoping into spec 02.

No open questions blocking that work. The stub is: `specs/02-llm/`
(requirements / design / tasks), anchored to `service-catalogue.md`.
