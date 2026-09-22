# muse-replica — all 9 phases complete: agent loop, memory, subagents, browser, scheduler, connectors, external API, production scale, client UI

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
  Never put raw credentials in tool arguments — Phase 6 added the vault
  (`connectors/`); the model works with connection ids only.
- `RunStore` is in-memory in Phase 1; the interface is keyed for a persistent
  backing store (the blueprint's outbox/event-sourced runs).

## What's next (per blueprint)

All ten build phases are complete (the blueprint's titled Phase 7, safety
hardening, was slotted in as Phase 10 after Phase 9). See [docs/SETUP.md](docs/SETUP.md) for
full setup/run instructions, and the blueprint's *Deployment blueprint*
section for the production topology (Docker Compose locally, Kubernetes
or equivalent in production). Remaining work is operational, not
architectural: real model provider keys, Postgres + vector extension,
Redis/durable queue, S3-compatible object store, and the staging
promotion gates.

## Phase 10 — safety hardening and adversarial readiness

```
  safety/
    __init__.py        # package exports
    risk_catalog.py    # full R0–R5 catalog accessor; verifies every tool
                       # in tool-capabilities.yaml maps to a documented class
    taint.py           # source-to-sink taint tracking: web/email/pdf/child
                       # output tainted at ingestion; tainted data at a
                       # sensitive sink needs explicit user clearance
    injection.py       # deterministic prompt-injection classifier
                       # (block/suspect/none) + deterministic destination
                       # checks (origin/recipient/path vs authorized intent)
    strong_auth.py     # second-factor attestation for R4 approvals:
                       # challenge -> out-of-band completion -> single-use,
                       # request-bound verification; guarded_resolve refuses
                       # grants without attestation
    secret_scan.py     # canary + secret-shape scanners gating persistence
                       # and egress of seeded credentials
    redteam.py         # red-team corpus runner + release gate: any fixture
                       # whose outcome differs from pinned expectation raises
                       # ReleaseBlocked
    support.py         # scoped, time-boxed, audited support grants;
                       # privacy-preserving diagnostics (counts/hashes/codes,
                       # never raw user data); run quarantine
    runbooks.md        # RB-1..RB-5 incident runbooks
    fixtures/
      redteam_corpus.yaml  # 14 attack/control fixtures
  demo_safety.py       # 43 checks proving the exit criteria
  policy/engine.py     # extended (additively): injection block, destination
                       # mismatch, strong-auth, taint clearance, and suspect-
                       # review rules ahead of the bound-approval rule
  policies/risk-catalog.yaml  # full catalog: description, examples,
                              # treatment, and approval per class
```

Phase 10 exit criteria, as demonstrated: injection fixtures across web,
email, PDF, and child output cannot create unauthorized effects
(INJECTION_BLOCKED); argument mutation invalidates the approval; a
cross-origin browser redirect triggers re-evaluation (CROSS_ORIGIN_REDIRECT);
secret scanners block persistence and egress of seeded credentials; the
red-team regression gate passes on the hardened pipeline and fails loudly
against a deliberately permissive policy — authorization regressions block
release. All Phase 1–9 demos remain green.

## Phase 9 — client UI replication

```
  client/
    __init__.py       # package exports
    api_client.py     # OpenMuseClient: sessions, idempotent sends, SSE +
                      # Last-Event-ID resume, approval decisions, run
                      # cancellation, artifact up/download, offline queue,
                      # read-only cached views
    sse.py            # SSE parsing + SSEClient with reconnect cursors
    http.py           # stdlib HTTP helper, HttpError
    state.py          # ephemeral client state (drafts, cursors, queue,
                      # cached views) + secret-leak scanner/assertions
    approvals.py      # ApprovalCard: exact destination/effect/bound
                      # argument hash; R4/R5 device-auth gating with
                      # re-display of bound fields after auth
    services.py       # surface wrappers: ScheduleClient, MemoryClient
                      # (viewer/editor + forget pipeline), ConnectorClient
                      # (catalog, OAuth, vault capture — never raw secrets),
                      # BrowserClient (handoff), UsageClient (quotas/privacy)
    domains_client.py # GoalsClient / FeedClient / IdeasClient
    serve_ui.py       # reference client server: static web UI, /v1/*
                      # reverse proxy to the real API, /v1/local/* domain
                      # endpoints, server-side Secure Vault capture page
    web/
      index.html      # single-page app shell (skip link, ARIA landmarks)
      css/tokens.css  # original token-based design system (light/dark)
      css/app.css     # application styles, :focus-visible, responsive
      js/openmuse-api.js  # JS API layer: idempotency keys, fetch-based
                      # SSE with resume, approval binding, WebAuthn device
                      # auth for high-risk approvals, vault-capture flow,
                      # degraded-mode queue
      js/ui.js        # views: chat (+side chats), approvals, feed, goals,
                      # library/artifacts, ideas, memory, schedules/hooks,
                      # connectors, usage/privacy, voice (Web Speech API)
  domains/
    goals.py feed.py ideas.py   # file-backed Goals/Feed/Ideas stores
  demo_client.py      # 102 checks: client layer against the real backend
                      # (idempotency, SSE + reconnect, approval binding,
                      # mutated-argument rejection, device-auth gating,
                      # artifacts, schedules/hooks, connectors + vault
                      # capture, memory + forget, goals/feed/ideas,
                      # browser handoff, usage/export, offline queue,
                      # no-secret scans, web-UI serving/validity/branding/
                      # a11y/contrast checks)
```

Phase 9 exit criteria, as demonstrated: every backend capability from
Phases 1–8 is reachable from the client; approval cards display exact
destination, effect, and the bound argument hash, and any mutation of the
arguments invalidates the approval (409); high-risk (R4/R5) approvals
require device authentication and re-display the bound fields afterwards;
no secret, token, or credential material appears in client state, storage,
or logs (asserted by scanner); keyboard navigation, screen-reader labels,
and ≥ 4.5:1 text contrast pass on the primary screens; the client ships
zero Meta trademarks, logos, or proprietary assets — original "OpenMuse"
wordmark and iconography only.

Note: the reference web client is a zero-dependency static app (same
project convention as the backend: stdlib only, no build step) rather
than React+TypeScript; it implements the same API-layer contracts, so a
React/TS port can drop in over `js/openmuse-api.js` unchanged.

## Phase 8 — production scale, resilience, and multi-surface polish

```
  production/
    queue.py          # durable regional queues: idempotent submit (message-id
                      # dedup), claim/ack/nack, fail_region + drain_region
                      # failover that never moves completed/dead work
    workers.py        # worker pool off the API process: exactly-once
                      # execution, bounded retries, dead-lettering,
                      # quota backpressure before any execution
    quotas.py         # per-tenant call/token/spend budgets in sliding
                      # windows; shed-or-queue backpressure; per-provider
                      # unit-cost accounting
    routing.py        # tenant provider ordering over the gateway Router;
                      # fallbacks only when data policy is compatible
    channels.py       # ChannelAdapter (mock messaging), signed approval
                      # deep links consistent across web/mobile/messaging
    dr.py             # BackupService (checksummed snapshots, RTO/RPO
                      # restore exercises) + TenantDeletionService
                      # (delete everywhere, zero-residual audit)
    adapters.py       # BackupParticipant adapters: database, objects,
                      # vectors, vault, browser profiles, memory,
                      # schedules, queue, backups
    objects.py        # durable tenant-scoped object store ("objects" tier)
    runlog.py         # durable per-tenant run-record log ("database" tier)
    loadtest.py       # scripted concurrency/latency targets, pass/fail
    metrics.py        # per-tenant counters/timings -> observability events
    namespace.py      # production.* tools (quota_status R1, backup_now R2,
                      # request_export R2, restore R3, delete_tenant R4
                      # destructive, channel_send R2)
  demo_production.py  # 72 checks: queue/workers, idempotent replay,
                      # regional failover, quotas, routing, channels,
                      # deep links, deletion audit, backup/restore,
                      # load test, metrics, tool + policy wiring
```

Phase 8 exit criteria, as demonstrated: regional failure re-queues in-flight
work without corrupting run state; queue replay never double-executes;
restore exercises report RTO/RPO against objectives; tenant deletion
audits zero residuals across all nine tiers; the load harness reports
against declared targets.

## Phase 7 — external API

```
  api/
    server.py         # stdlib HTTP server: versioned /v1 routes, bearer
                      # API-key auth + scopes, per-key rate limiting,
                      # idempotency-key replay, SSE event streams with
                      # Last-Event-ID reconnect, machine-readable errors,
                      # per-request audit log
    backend.py        # service layer over the platform: sessions, message
                      # submission driving real turns in background threads,
                      # approval decisions (argument-hash bound), run cancel,
                      # artifact store; honors RunStore idempotency, the
                      # deterministic policy engine, and ManualDecider parks
    auth.py           # scoped API keys (omk_...); only hashes stored
    ratelimit.py      # per-key token buckets -> 429 + Retry-After
    idempotency.py    # same key + same fingerprint replays; same key +
                      # different body -> IDEMPOTENCY_KEY_REUSED
    eventbus.py       # per-run sequenced SSE events; reconnect replays
                      # only missed events
    webhooks.py       # run.completed/run.failed subscriptions, HMAC-signed
                      # deliveries, injectable transport
    openapi.py        # generated OpenAPI 3.0 doc incl. versioning and
                      # deprecation policy
    errors.py         # stable machine-readable error codes
  prompts/api-surface.md
```

Endpoints follow the blueprint's external API contracts:
`POST /v1/sessions`, `POST /v1/chats/{chat_id}/messages` (idempotent),
`GET /v1/runs/{run_id}/events` (SSE), `POST
/v1/approvals/{approval_id}/decision` (exact argument_hash required),
`POST /v1/runs/{run_id}/cancel`, artifact upload/download, webhooks, and
`/openapi.json`. Run `python3 demo_api.py` (36/36 checks): auth/scope
rejections, session + message + SSE approval flow, reconnect replay without
duplicates, idempotent retry, tamper-proof approval decision, artifact
round-trip, signed webhook delivery, 429 rate limiting, and cancel.

## Phase 6 — connectors and secure credential use

```
  connectors/
    registry.py       # ConnectorRegistry: manifest enforcement, connection
                      # flows, single-call credential handles, scope checks,
                      # terminal rate-limit hard stop, disconnect revocation,
                      # secret-leak scanning, observability events
    vault.py          # Vault seam + MemoryVault dev substitute: opaque
                      # vault:// refs bound to (tenant, provider, account,
                      # purpose); Secure Vault capture flow; leak scanning
    oauth.py          # mock OAuth 2.0 PKCE flow: state+verifier bound to
                      # tenant/scopes/expiry; server-side code exchange;
                      # token material vaulted, never returned
    rest.py           # RESTConnector base: error taxonomy (incl.
                      # RATE_LIMIT_TERMINAL), deterministic pagination;
                      # injectable Transport (MockTransport offline)
    github.py         # reference connector: get_repo (R0), star/unstar (R3
                      # external writes needing bound approvals)
    one_time_code.py  # protected OTP route: the agent sees only
                      # success/failure, never the code
    namespace.py      # connector.* tools: connect/complete_oauth (R2),
                      # status (R0), disconnect (R2), plus one tool per
                      # declared operation (connector.<name>.<op>)
  prompts/connector-operator.md
```

The model never handles raw secrets: tools take connection ids, the
registry resolves vault references into short-lived handles scoped to one
call, and adapter output is scanned for credential leaks before it reaches
the model. Scope expansion requires a new consent ceremony; a terminal
provider limit stops that account for the run; disconnect deletes vault
material and later calls fail closed.

Run `python3 demo_connectors.py` (54/54 checks): manifest declaration,
OAuth PKCE + API-key-capture flows, read under policy, scope enforcement,
approval-bound external writes (single-use, tamper-void), hard rate-limit
stop, leak guard, disconnect, protected one-time codes, scope-expansion
ceremony, tenant isolation, and all `connector.*` tools through the
registry.

## Phase 5 — scheduler, hooks, and proactive delivery

```
  scheduler/
    service.py        # ScheduleService: cron/one-shot schedules, versioned
                      # edits, misfire policies (skip/fire_once/catch_up),
                      # dedup keys, run_now, enable/disable, tick loop
    cron_expr.py      # pure-Python 5-field cron; timezone-aware next-fire with
                      # DST handling (spring-forward gaps skipped, fall-back
                      # ambiguous times fire once)
    hooks.py          # hook ingress: replay window, HMAC signature, event-ID
                      # dedup, payload filters; payloads are untrusted data
    delivery.py       # delivery critic: NOTIFY_NOW / ADD_TO_FEED /
                      # HOLD_UNTIL_QUIET_HOURS_END / SILENT_LOG with
                      # deterministic rules before the model (critical alerts
                      # can't be silenced; caps/quiet hours can't be overridden)
    runner.py         # bounded run executor: capability-ceiling intersection,
                      # same R0–R5 policy engine; ASK while the user is away
                      # fails closed (UNATTENDED_FAIL_CLOSED) and defers the
                      # approval; durable RunRecord history
    store.py          # file-backed durable state: schedules, instances, hooks,
                      # run history, event dedup, notification caps
    namespace.py      # scheduler.* tools: create (R2), list/get (R0),
                      # update/remove/run_now/enable/disable (R2),
                      # hook_create (R2), hook_list (R0)
  prompts/scheduler.md, prompts/delivery-critic.md
```

A yes to a one-time task authorizes exactly one run: one-shot schedules
fire once and auto-disable; a second execution without a new approval is
refused. Recurring schedules carry the user's approval text naming the
schedule; every instance snapshots the instruction, ceiling, and approval
scope at creation, and edits bump the version without mutating fired
instances.

Run `python3 demo_scheduler.py` (49/49 checks): DST previews, one-shot
single-fire, recurring cadence + removal, misfire policies, dedup, edit
versioning, disable, hook verification/dedup/replay, ceiling denial,
unattended fail-closed approvals, the delivery matrix, and all
`scheduler.*` tools through the registry.

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
