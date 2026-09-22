# muse-replica — Phase 1: core agent loop + tool runtime

A from-scratch reimplementation of the Muse-style personal-agent architecture,
built from the build blueprint (`../your_files/muse-replica-build-blueprint/`).
Phase 1 covers the **durable single-agent turn engine** and the **tool runtime**
with a provider-neutral model gateway. Later phases (memory layers, skills +
subagents, browser computer use, scheduler, connectors) plug into the seams
left here.

## Layout

```
muse-replica/
  demo.py                 # smoke test: one full turn against the mock provider
  requirements.txt
  prompts/
    main-agent.md         # versioned system prompt (main-agent@0.1.0)
    tool-choice-addendum.md
    context-label-wrapper.md
  policies/
    risk-catalog.yaml     # R0-R5 risk classes
    tool-capabilities.yaml# tool -> risk / capability / side-effect mapping
  agent/
    models.py             # Run, RunBudgets, RunStore; validated state transitions
    context_builder.py    # deterministic context assembly + manifest
    turn_engine.py        # the turn state machine (advance_run)
    seams.py              # Phase 3/4/5 interfaces (subagents, browser, scheduler)
  gateway/
    protocol.py           # normalized ModelRequest/Response, error taxonomy
    router.py             # model-class routing, fallback, per-step idempotency
    providers/
      mock.py             # ScriptedMockProvider / ProgrammableMockProvider (offline)
      openai_compat.py    # real OpenAI-compatible adapter (env-configured)
  tools/
    registry.py           # namespaces + deferred loading (catalog -> load_namespace)
    executor.py           # tool-call lifecycle: validate, hash, policy, execute, redact
    redaction.py          # secret patterns; never echo raw secrets to the model
    namespaces/
      system_tools.py     # system.clock, tools.load_namespace
      math_tools.py       # math.calc (safe AST eval)
      file_tools.py       # files.read/write/list (jailed to workspace)
      shell_tools.py      # shell.exec (jailed, destructive-command guard)
      web_tools.py        # web.fetch (public read; output labeled untrusted)
      memory_tools.py     # memory.note (seam for Phase 2 layered memory)
  policy/
    engine.py             # deterministic R0-R5 policy -> ALLOW / ASK / DENY
    approvals.py          # bound approval grants; ManualDecider / AutoApproveDecider
  memory/
    store.py              # Phase 1 note store (replaced by Phase 2 layers)
  observability/
    events.py             # immutable event envelopes per state change
```

## How it maps to the blueprint

| Blueprint section | Implementation |
|---|---|
| Durable turn state machine | `agent/turn_engine.py` — all 11 canonical states, validated transitions |
| Deterministic context assembly | `agent/context_builder.py` — authority-ordered layers, typed blocks, manifest |
| Model gateway | `gateway/` — normalized protocol; Muse Spark is one swappable adapter |
| Deferred namespaces | `tools/registry.py` — catalog first, `tools.load_namespace` on demand |
| Tool-call lifecycle | `tools/executor.py` — prevalidate, canonicalize, hash, policy, execute, redact |
| Sentinel-style safety | `policy/engine.py` — deterministic R0–R5; the model proposes, policy disposes |
| Approval binding | `policy/approvals.py` — grants bound to exact argument hash, single-use |
| Prompt-injection defense | typed data blocks + `<external_data>`-style wrappers; policy enforces destination rules |
| Event envelope | `observability/events.py` — every state change appended immutably |

## Run the smoke test

```bash
cd ~/workspace/muse-replica
pip install -r requirements.txt   # jsonschema, requests, pyyaml
python3 demo.py
```

Expected: `ALL CHECKS PASSED` and exit code 0. The demo runs one full turn —
namespace discovery, an R0 read (`system.clock`), an R2 write (`files.write`
through ASK → auto-approved by the **demo-only** `AutoApproveDecider`), result
ingestion as untrusted data, and a final answer — plus safety checks
(redaction, unknown-tool rejection, destructive-command guard, fail-closed
policy, duplicate-submission idempotency).

## Plug in a real model

```bash
export OPENAI_API_KEY=...            # any OpenAI-compatible endpoint works
export OPENAI_BASE_URL=https://api.openai.com/v1   # or vLLM / Ollama / Azure
export OPENAI_MODEL=gpt-4o-mini      # pin a version for evaluations
```

```python
from gateway import Router
from gateway.providers import OpenAICompatProvider
router = Router(routes={
    "planner": [OpenAICompatProvider()],
    "fast":    [OpenAICompatProvider(model="gpt-4o-mini")],
})
# pass `router` into agent.turn_engine.Deps instead of the mock router
```

`openai_compat.py` maps HTTP failures into the gateway's stable error taxonomy
(`TRANSIENT`, `RATE_LIMIT`, `INVALID_REQUEST`, `CONTEXT_OVERFLOW`,
`AUTH_FAILURE`, `SAFETY_BLOCK`); only transient/rate-limit errors fall over to
the next provider in the route.

## Production notes (not demo behavior)

- `AutoApproveDecider` is **demo/test only**. Production uses `ManualDecider`,
  which parks the run in `WAITING_FOR_APPROVAL` until a client resolves the
  approval card. Re-entering `advance_run` afterwards rebuilds context and the
  model re-proposes; still-valid bound grants are honored without re-asking.
- Tool results are labeled `trust="untrusted"`; the system prompt and the
  policy engine both treat them as data, never instructions.
- Secrets are redacted from every model-visible view (`tools/redaction.py`).
  Never put raw credentials in tool arguments — Phase 6 adds the vault.
- `RunStore` is in-memory in Phase 1; the interface is keyed for a persistent
  backing store (the blueprint's outbox/event-sourced runs).

## What's next (per blueprint)

- **Phase 2** — layered memory (`memory/store.py` is the seam), compaction, identity files
- **Phase 3** — skills + subagents (`agent/seams.py::SubagentRunner`)
- **Phase 4** — browser computer use (`agent/seams.py::BrowserOperator`)
- **Phase 5** — crons + hooks (`agent/seams.py::Scheduler`)
