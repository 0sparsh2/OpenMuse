# muse-replica — Phase 3: skills + subagents (on the Phase 1 agent loop + Phase 2 memory)

A from-scratch reimplementation of the Muse-style personal-agent architecture,
built from the build blueprint (`../your_files/muse-replica-build-blueprint/`).
Phase 1 covers the **durable single-agent turn engine** and the **tool runtime**
with a provider-neutral model gateway. Phase 2 adds the **layered memory
system**. Later phases (skills + subagents, browser computer use, scheduler,
connectors) plug into the seams left here.

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

- **Phase 4** — browser computer use (`agent/seams.py::BrowserOperator`)
- **Phase 5** — crons + hooks (`agent/seams.py::Scheduler`)

## Phase 3 — skills and subagents

```
  subagents/
    models.py         # DelegationRecord, ChildBudget, ChildResult, join policies
    skills.py         # skill catalog, loader, validator, hybrid selection,
                      # version pinning; only selected skill text enters context
    runner.py         # SubagentRunner: spawn/status/send/close/cancel, child
                      # turn loop, capability intersection, budget carving,
                      # depth caps, parent-routed approvals, memory isolation
    coordinator.py    # fan-out/fan-in (all/any/quorum), pipeline joins,
                      # typed parent synthesis with evidence_refs
    namespace.py      # subagent.* tools: spawn (R2), list/status (R0),
                      # send (R2), close (R1)
  skills/
    workspace-survey/ # reference skill: files.* codebase inventory
    memory-hygiene/   # reference skill: memory.* consolidation
  prompts/subagent-child.md  # the blueprint's child system prompt
  prompts/coordinator.md     # parent synthesis rules
```

Run `python3 demo_subagents.py` (24/24 checks): spawn → tools under policy →
typed handoff → close; fan-out/fan-in + pipeline; ceiling denials; skill
selection; depth-loop blocking; cancellation propagation; budget carving;
child memory isolation; send() refinement.

## Phase 2 — layered memory

```
  memory/
    layered.py        # facade: remember / recall / forget across all layers
    records.py        # MemoryRecord, JournalEntry, DerivationEdge, candidates
    curated.py        # structured durable records + MEMORY.md projection + hybrid recall
    journal.py        # episodic daily notes (append-only, event-linked)
    people.py         # relationship pages + conservative entity resolution
    vector_index.py   # file-backed cosine-similarity index (pgvector seam)
    embeddings.py     # EmbeddingProvider; deterministic offline embedder;
                      # OpenAICompatibleEmbedder for production (OPENAI_API_KEY)
    consolidation.py  # deterministic extractor + consolidator
                      # (add / reinforce / refine / supersede / journal_only / reject)
    forgetting.py     # plan -> delete/tombstone -> regenerate -> audit -> verify
    derivation.py     # forget graph: message -> candidate -> memory -> projection
    working.py        # turn-scoped scratchpad, injected into context as a block
    maintenance.py    # background maintainer interface (scheduler seam)
    store.py          # Phase 1 MemoryStore interface, now layered-backed
  tools/namespaces/memory_tools.py  # memory.note (R2), memory.recall (R1),
                                    # memory.forget (R2, plan-first, ambiguous-safe)
  prompts/memory-extractor.md, memory-consolidator.md, memory-maintenance.md,
          forget-planner.md, recall-planner.md, compactor.md
```

Recall ranking follows the blueprint's hybrid formula
(0.35 semantic + 0.20 lexical + 0.15 recency + 0.15 authority +
0.10 commitment + 0.05 user-confirmed, minus duplicate/contradiction
penalties) with MMR diversification. Superseded records are excluded unless
`include_history=True`. Forgetting is plan-first: ambiguous targets are never
deleted; tombstone is the default (non-content audit marker retained).

Run the memory verification: `python3 demo_memory.py` (35 checks).
