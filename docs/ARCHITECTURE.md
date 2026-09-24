# OpenMuse architecture

How the pieces fit together, how a message becomes an answer, and how the
safety model works. The second half keeps the original 10-phase build record
the platform grew from.

## The big picture

```
 browser / PWA  ──►  client/serve_ui.py  ──/v1 proxy──►  api/server.py  ──►  api/backend.py
 (client/web)        static app, SW, push                auth, routes, SSE     service layer
                                                                                   │
            ┌──────────────────────────┬───────────────────────┬──────────────────┼──────────────────────┐
            ▼                          ▼                       ▼                  ▼                      ▼
   agent/turn_engine.py        tools/ (registry,       memory/service.py   browser/live_operator   connectors/
   state machine per run       executor, namespaces)   per-user layers     live Chromium           composio_bridge
            │                          │                                        │
            ▼                          ▼                                        ▼
   agent/context_builder.py    policy/ engine +        search/ (web search,  Jev page checks
   prompt + memory + notes     approvals (R0–R5)       router, Jev, guard)
            │
            ▼
   gateway/ ── OpenAI-compatible provider ──► NVIDIA NIM (model, embeddings, Riva speech)
```

`serve_nim.py` wires everything together: providers, memory, browser, Composio,
voice, schedules, monitors, proactive ideas, saved-login vault and push. It then
starts the API. SQLite (`storage/db.py`) persists chats, runs, events,
approvals, notifications and per-user settings, so nothing is lost on restart.

## A turn, step by step

1. **Submit.** `POST /v1/chats/{id}/messages` creates a run
   (`api/backend.py::submit_message`). The search router starts in the
   background (rules first, then Jev), and voice turns register a token stream.
2. **Build context** (`agent/context_builder.py`), in order: the system prompt
   (`prompts/main-agent.md` plus tool guidance) → the user's memory (profile,
   recalled facts, working memory) → the chat's history summary → the new
   message → trusted runtime notes (e.g. "look this up first") → the voice note,
   last, for voice turns. Tool schemas come only from loaded namespaces.
   `system`, `files`, `task`, `web` and `memory`, plus connected apps, load by
   default; others load on demand (`tools.load_namespace`).
3. **Call the model** (`gateway/`). The request goes through the
   OpenAI-compatible provider. `gateway/resilience.py` handles retries with
   backoff, a fallback model, and one retry with thinking off when the model
   comes back empty (it can spend its whole budget thinking on a long tool
   result). Errors that NIM reports inside a 200 stream are retried too. Every turn
   streams its words as they're written: `gateway/streaming.py` groups tokens
   into short chunks and sends them as `assistant.partial` events, which aren't
   stored individually. A retry after a dropped connection sends a reset, and a
   stream that ends without the model's end signal counts as a failure, not as
   a finished answer.
4. **Evaluate proposed tool calls** (`agent/turn_engine.py`). Each call is
   validated against its schema, hashed, and checked by the policy engine
   (`policy/engine.py`), which returns ALLOW, ASK or DENY.
5. **Approvals.** ASK parks the run (`WAITING_FOR_APPROVAL`) and sends an
   approval card over SSE. The grant is bound to the exact argument hash and is
   single-use. With autonomy on, `AutonomousDecider` approves reversible local
   R1/R2 steps. When you deny, the model is told so in a trusted runtime notice,
   and if it proposes the identical action again in the same run, it is refused
   straight away (`USER_DECLINED`) rather than asking you again.
6. **Execute** (`tools/executor.py`). Tools run with a deadline. Their output is
   schema-checked, capped, scanned for secrets, and handed back to the model as
   **untrusted data**.
7. **Loop** until the model answers. Then citations are verified (for web
   answers), the answer streams out, a receipt and notification are recorded, and
   memory learns from the turn in the background.

## Main parts

| Part | Where | Notes |
|---|---|---|
| Turn engine | `agent/` | 11 validated states, budgets (model calls, tool calls, wall time), pause/resume/cancel/retry |
| Tools | `tools/registry.py`, `tools/executor.py`, `tools/namespaces/` | Namespaces with deferred loading; each tool declares risk, side effect, idempotency and an optional result card |
| Policy | `policy/`, `policies/tool-capabilities.yaml`, `policies/risk-catalog.yaml` | Deterministic R0–R5; unknown tools fail closed (R5); `ask_for_external_writes` asks for R3 instead of denying |
| Model gateway | `gateway/` | Normalised protocol, error taxonomy, per-run streaming sinks (`gateway/streaming.py`) |
| Memory | `memory/service.py` (+ `memory/*`) | Per-user profile, curated records, journal, people pages, knowledge bank; LLM extraction with thinking off; NIM embeddings |
| Web search | `search/`, `tools/namespaces/web_tools.py` | See [WEB_SEARCH.md](WEB_SEARCH.md) |
| Live browser | `browser/live_operator.py`, `browser/namespace.py` | One worker thread with Playwright; per-user profiles; frames streamed to the UI; take control; commit barrier; saved logins (`browser/logins.py`); downloads to the Library; Jev page checks |
| Apps | `connectors/composio_bridge.py`, `api/mail.py` | Curated Gmail and Calendar tools with risk levels; email and calendar cards; attachments; new-mail alerts |
| Library | `api/library.py` | Upload, read, fill PDF forms, create md/pdf/csv/xlsx; malicious-PDF guard |
| Proactive | `api/scheduling.py`, `api/monitors.py`, `api/proactive.py` | Per-user cron, monitors, goals, ideas, feed → notifications and web push (`api/push.py`) |
| Helpers | `api/parallel.py`, `subagents/` | 2–3 parallel children with carved budgets and restricted tools |
| Voice | `api/voice.py`, `client/web/js/ui.js` (VoiceMode) | Riva ASR/TTS, VAD, speculative ASR, sentence-chunked speech, barge-in |
| Accounts | `api/accounts.py` | scrypt passwords, lockout, session tokens bound to the user |
| External API | `api/server.py` | Versioned `/v1`, scoped keys, rate limits, idempotency, SSE with resume, `/openapi.json` |
| Web app | `client/web/` | Static SPA: chat, drawer pages, cards, approvals, live browser viewer, PWA (`sw.js`, manifest) |

## Safety

The model proposes; deterministic code decides. The rules, from the inside out:

- **Risk classes.** Each tool has a risk class in `policies/tool-capabilities.yaml`:
  - R0: read-only.
  - R1: private reads.
  - R2: reversible local writes.
  - R3: external writes, e.g. sending email or creating events.
  - R4: money and irreversible actions.
  - R5: unknown, and denied.

  A classifier can raise a tool's risk but never lower it.
- **Approvals.** Grants are bound to the exact argument hash and single-use. They
  are consumed after the tool re-checks them, so a changed action can't reuse one.
  Autonomy only covers reversible local R1/R2 steps. Over voice, only R0–R2 can be
  approved; the server refuses anything higher with `CONFIRM_ON_SCREEN`.
- **Commit barrier.** Clicks that buy, book or pay become commit proposals and are
  not executed until approved. Jev can flag extra ones, such as a "Continue" button
  on a payment page, but it can never un-flag one.
- **Untrusted content.** Web pages, emails, PDFs and child-agent output are tainted
  data wrapped as untrusted. `safety/injection.py` and `safety/taint.py` block
  tainted data from reaching sensitive sinks without clearance. There are red-team
  fixtures for "forward my inbox"-style injections.
- **Network guard.** `search/netguard.py` blocks loopback, private, link-local and
  `.local` addresses and embedded credentials, and re-checks every redirect.
- **Secrets.**
  - Saved passwords are Fernet-encrypted. The model only ever sees
    `vault://login/<id>` references, and the browser worker fills the real value,
    only on the matching origin, with approval.
  - Composio keeps the OAuth tokens.
  - Every model-visible view is scanned and redacted (`tools/redaction.py`).
- **Isolation.** Memory, chats, files, logins, browser profiles, connected apps,
  push subscriptions and settings are all keyed by user. Session tokens can only
  reach their own user's resources.
- **Release gate.** `demo_safety.py` runs the red-team corpus and fails loudly
  against a deliberately permissive policy, so authorization regressions block
  release.

## Build history: the original 10 phases

OpenMuse started as a from-scratch build of a Muse-style agent platform, following
a written blueprint in 10 phases. The phase notes below are kept as they were
written, as a record of the design; the sections above describe the current system.

### Phase 1 — durable turn engine and tool runtime

Layout:

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

How it maps to the blueprint:

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

### Phase 10 — safety hardening and adversarial readiness

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

### Phase 9 — client UI replication

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

### Phase 8 — production scale, resilience, and multi-surface polish

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

### Phase 7 — external API

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

### Phase 6 — connectors and secure credential use

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

### Phase 5 — scheduler, hooks, and proactive delivery

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

### Phase 3 — skills and subagents

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

### Phase 2 — layered memory

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
