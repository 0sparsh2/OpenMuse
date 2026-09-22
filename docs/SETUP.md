# OpenMuse — setup and run instructions

Full-platform guide for the completed 9-phase build. Everything runs on
Python 3.10+ with the standard library plus `requirements.txt`; there is
no build step and no framework.

## 1. Prerequisites

- Python 3.10+ (developed on 3.12)
- `pip install -r requirements.txt` (`pyyaml`, `jsonschema`, `requests`)
- Node.js (optional, only to re-run the client JS syntax check:
  `node --check client/web/js/openmuse-api.js client/web/js/ui.js`)
- A model provider API key for live (non-demo) runs — see §4.
  Without one, every component still runs against the built-in scripted
  mock provider, which is what the demo scripts use.

## 2. Install

```bash
git clone https://github.com/0sparsh2/OpenMuse.git
cd OpenMuse
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

No other installation is needed. All state is file-backed under data
directories you choose at startup (or `tempfile` in the demos); nothing
writes outside the paths you pass in.

## 3. Configuration

| Area | How to configure |
|---|---|
| Model provider | `gateway/providers/openai_compat.py` reads `OPENAI_API_KEY`, `OPENAI_BASE_URL` (default `https://api.openai.com/v1`), `OPENAI_MODEL` (default `gpt-4o-mini`). It speaks the OpenAI-compatible chat API, so any compatible endpoint works. |
| Policy | `policies/tool-capabilities.yaml` (per-tool risk classes R0–R5) and `policies/risk-catalog.yaml`. The classifier may raise risk, never lower it. |
| Prompts | `prompts/` — layer prompts (main agent, subagent child, memory extractor/consolidator, scheduler, browser operator, connector operator, delivery critic, …). |
| API keys (clients) | Created programmatically: `backend.keys.create_key(name=..., scopes={...}, tenant_id=...)`. Scopes: `sessions:write/read`, `runs:write/read`, `approvals:read/decide`, `artifacts:write/read`, `webhooks:write`, `admin`. |
| Webhooks | `POST /v1/webhooks` with a target URL; deliveries are HMAC-signed (see `api/webhooks.py`). |

Secrets discipline: raw credentials live only in the Secure Vault
(`connectors/vault.py`; `MemoryVault` in-process, swap for a real KMS in
production). The agent, the API, and the client only ever handle opaque
`credential_ref`s and capture ids. Never put secrets in tool arguments,
prompts, logs, or client state — `client/state.py::assert_no_secrets`
and `tools/redaction.py` enforce this.

## 4. Run each component

### 4.1 Agent turn engine (Phase 1)

```python
from agent.turn_engine import ...
```

The engine is embedded by the API backend; to drive it directly see
`demo.py` (14 checks: the full turn loop against the mock provider).

### 4.2 External API server (Phase 7)

```python
from api import ApiBackend, serve

backend = ApiBackend(workspace_root="/data/openmuse",
                     respond=my_provider_respond)  # or omit -> mock
server = serve(backend, host="127.0.0.1", port=8000)
```

- `GET /v1` → version; `GET /openapi.json` → full spec.
- `POST /v1/sessions` → create a chat session.
- `POST /v1/chats/{chat_id}/messages` with `Idempotency-Key` → starts a run.
- `GET /v1/runs/{run_id}/events` → SSE (`run.status`, `assistant.delta`,
  `approval.required`, `run.completed`); send `Last-Event-ID` (or
  `?last_event_id=`) to resume without duplicates.
- `GET /v1/approvals/{id}` → the approval card payload (tool, risk,
  `argument_hash`, `presentation` bind fields).
- `POST /v1/approvals/{id}/decision` with `{"decision": "approve"|"deny",
  "argument_hash": "<exact hash>"}` → wrong hash is 409; approving a
  parked run resumes it.
- `POST /v1/runs/{run_id}/cancel` → cancel a parked/running run.
- `POST /v1/artifacts` / `GET /v1/artifacts/{id}` → artifact upload/download.
- `GET|POST /v1/webhooks`, `DELETE /v1/webhooks/{id}` → signed deliveries.

To use a live model instead of the mock, pass a `respond` callable built
on `gateway/providers/openai_compat.py` (or add a provider under
`gateway/providers/` and register it in `gateway/router.py`).

### 4.3 Reference web client (Phase 9)

```bash
# terminal 1 — API backend
python3 - <<'EOF'
from api import ApiBackend, serve
backend = ApiBackend(workspace_root="/data/openmuse")
rec, key = backend.keys.create_key(name="ui", scopes={"admin"},
                                   tenant_id=backend.tenant_id)
print("API_KEY=" + key)   # paste into the client's key box
serve(backend, host="127.0.0.1", port=8000)
import time; time.sleep(10**6)
EOF

# terminal 2 — client server (static UI + /v1 proxy + local surfaces)
python3 -m client.serve_ui --api http://127.0.0.1:8000 --port 8080 \
    --data /data/openmuse-ui
# open http://127.0.0.1:8080, paste the API key in the sidebar box
```

What you get: main chat with streaming + tool-call cards + expandable run
timeline, side chats, approval cards (destination/effect/bound hash;
R4/R5 require WebAuthn device auth), Feed, Goals, Library/artifacts,
Ideas, memory viewer/editor with forget controls, schedule/hook manager,
connector catalog with Secure Vault capture flows, usage/privacy
controls, and a voice surface (Web Speech API where available).

Programmatic client (same contracts, for scripts/mobile):

```python
from client import OpenMuseClient
c = OpenMuseClient("http://127.0.0.1:8000", api_key)
s = c.create_session("hello")
r = c.send_message(s["chat_id"], "summarize my day")
for ev in c.stream_run(r.run_id,
                       stop_when=lambda e: e.name == "run.completed"):
    ...
card = c.get_approval(approval_id)          # exact destination/effect/hash
c.decide_approval(card, "approve", device_auth=...)  # device auth for R4/R5
```

Per-surface wrappers live in `client/services.py` (`ScheduleClient`,
`MemoryClient`, `ConnectorClient`, `BrowserClient`, `UsageClient`) and
`client/domains_client.py` (`GoalsClient`, `FeedClient`, `IdeasClient`).

### 4.4 Scheduler & hooks (Phase 5)

```python
from scheduler.store import ScheduleStore
from scheduler.service import ScheduleService
svc = ScheduleService(ScheduleStore("/data/openmuse/schedules"))
svc.create_schedule(name="brief", schedule="0 9 * * *",
                    timezone="America/New_York", instructions="...")
svc.create_hook(name="gh", provider="github", event_type="push",
                instructions="...")
# drive due schedules: svc.tick(now) -> job instances -> run via the agent
```

Delivery policy (notify / feed / quiet-hours) is enforced by
`scheduler/delivery.py`. The client manages schedules/hooks from the
Schedules tab.

### 4.5 Connectors (Phase 6)

```python
from connectors.vault import MemoryVault
from connectors.oauth import OAuthFlow
from connectors.registry import ConnectorRegistry
from connectors.github import GitHubConnector, mock_github_transport
reg = ConnectorRegistry(vault=MemoryVault(), oauth=OAuthFlow())
reg.register_connector(GitHubConnector(mock_github_transport()))
```

Reference connector: GitHub (OAuth PKCE + API-key via vault capture).
OAuth: `reg.connect(..., auth_kind="oauth_pkce")` → `authorization_url`;
user authorizes; `reg.complete_authorization(...)` finishes. API key:
vault `create_capture` → user pastes the key on the Secure Vault capture
page (`/vault/capture/{id}` on the UI server) → `reg.connect(...,
auth_kind="api_key", capture_id=...)`. The model/client only ever sees
connection ids and credential refs. Rate-limit hard stop: a 429/terminal
limit halts the connector for the task — see `connectors/rest.py`.

### 4.6 Browser computer use (Phase 4)

`browser/operator.py` (`ManagedBrowserOperator`) — observation-grounded
actions, challenge detection with user handoff (`mark_challenge_resolved`
records the *user's* out-of-band resolution; the agent never solves
challenges), commit barrier with approval-bound proposals, no-evasion
enforcement. Wired as `browser.*` tools; surfaced in the client via
`BrowserClient` handoff cards.

### 4.7 Memory (Phase 2)

```python
from memory.layered import LayeredMemory
mem = LayeredMemory("/data/openmuse/memory")
mem.remember("My preferred dinner time is 7pm or later.")
mem.recall("dinner time")
mem.forget("dinner time")          # plan -> execute -> verify; tombstones
```

Journal (episodic), curated records (semantic, hybrid recall), people
pages, and the forgetting pipeline with audit log. Manage from the
client's Memory tab (viewer, source links, forget with plan preview).

## 5. Verify

Each phase has a self-contained demo (stdlib + mock provider, temp dirs):

```bash
python3 demo.py             # Phase 1: agent loop            (14 checks)
python3 demo_memory.py      # Phase 2: layered memory       (35 checks)
python3 demo_subagents.py   # Phase 3: subagents            (24 checks)
python3 demo_browser.py      # Phase 4: browser              (31 checks)
python3 demo_scheduler.py   # Phase 5: scheduler/hooks      (49 checks)
python3 demo_connectors.py  # Phase 6: connectors           (54 checks)
python3 demo_api.py         # Phase 7: external API          (36 checks)
python3 demo_production.py  # Phase 8: production scale     (72 checks)
python3 demo_client.py      # Phase 9: client UI            (102 checks)
```

Exit code 0 + `ALL CHECKS PASSED` = green. `demo_client.py` boots the
real API and UI servers and exercises the whole client surface over HTTP.

## 6. Deployment notes

The blueprint's *Deployment blueprint* section is authoritative; the
local equivalents of each tier in this repo:

| Production tier | This repo (local) | Swap for production |
|---|---|---|
| API/control | `api/serve()` (stdlib HTTP) | ASGI server, stateless replicas |
| Run queue | `production/queue.py` (file-backed) | Redis / durable queue |
| Database | `production/runlog.py`, file stores | PostgreSQL (+ pgvector for `memory/vector_index.py`) |
| Object store | `production/objects.py`, API artifacts | S3-compatible store; signed download URLs |
| Workers | `production/workers.py` | autoscaled agent/tool worker pools |
| Browser fleet | `browser/operator.py` (mock pages) | isolated Chromium pool, session affinity |
| Vault | `connectors/vault.py` (`MemoryVault`) | KMS-backed vault; keep the capture-page flow |
| Policy service | `policy/engine.py` (fail-closed) | independently deployed, low-latency |
| Hook ingress | `scheduler/hooks.py` (`HookIngress`) | public minimal endpoint + signature verify |

Operational runbooks (provider outage, unknown external-write outcome,
browser compromise, memory poisoning, credential exposure) are in the
blueprint's *Operational runbooks* section; the code hooks they assume
(`UNKNOWN_OUTCOME` marking, forensic preservation, vault revocation)
exist in `tools/`, `browser/`, `memory/`, and `connectors/`.

Production hardening checklist before any private beta: real provider
keys with per-tenant quotas (`production/quotas.py`), signed approval
deep links (`production/channels.py`), backup/restore exercises
(`production/dr.py`), tenant deletion audits, TLS + envelope encryption,
separate staging credentials, and the product/security acceptance
criteria in the blueprint's final section (all demonstrated by
`demo_production.py` + `demo_client.py` except the live-infrastructure
items).
