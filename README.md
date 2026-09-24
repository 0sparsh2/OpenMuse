# OpenMuse

An open-source personal agent in the style of Meta's Muse. It chats, searches
the web with citations, uses a real browser you can watch and take over,
remembers you, reads your email and calendar, and does tasks on a schedule.
It runs locally on **NVIDIA NIM** models. Anything that spends money, sends
something, signs in, or can't be undone waits for your approval.

- **Stack:** Python (standard library plus a few packages), no web framework.
  The UI is a static single-page app with no build step.
- **Models:** NVIDIA NIM through an OpenAI-compatible API. The default is Nemotron.
  NIM also provides embeddings (memory, search ranking) and Riva speech (voice).
- **Status:** 24 test suites; every check passes except the live voice-latency
  one, which varies with NVIDIA's hosted speech service. The roadmap is tracked
  in [issue #21](https://github.com/0sparsh2/OpenMuse/issues/21).

## What it can do

| Area | What you get |
|---|---|
| **Chat** | Answers stream in as they're written, one-line tool steps, result cards, task plans with pause/resume/retry, chat history per user. |
| **Web search** | ChatGPT-style search: parallel queries with recency filters, pages actually read, answers with clickable citations and a Sources panel, weather cards. Keyless, using DuckDuckGo. Automatic when a question needs it; can be turned off in Apps. See [docs/WEB_SEARCH.md](docs/WEB_SEARCH.md). |
| **Computer use** | A real Chromium browser, streamed live. "Take control" lets you drive it yourself. It pauses for human checks, and purchases or bookings need your approval (commit barrier). Saved logins are encrypted and never shown to the model. Downloads go to your Library. |
| **Memory** | Per-user layered memory: profile, curated facts, journal, people pages, and a document knowledge bank with NIM embeddings. It learns from conversations, and you can view it and make it forget. |
| **Apps** | Gmail and Google Calendar via Composio: search, read, draft, and send or invite (sending always asks). Attachments go to the Library, with optional new-mail alerts. Calendar has agenda and free-time cards. |
| **Library** | Upload, read and fill PDFs; create Markdown, PDF, CSV or XLSX documents; research reports keep their sources. |
| **Proactive** | Schedules (cron, per user and timezone), monitors (price drops, text appears, any change), goals with milestones, and ideas the agent suggests from your context. Sent as notifications. |
| **Helpers** | Parallel sub-agents for independent sub-tasks (compare three sites at once), with their own budgets and restricted tools. |
| **Voice** | Hands-free voice mode: Riva ASR/TTS on NIM, streamed answers spoken sentence by sentence, and barge-in (talk over it to interrupt). |
| **Mobile** | Installable PWA: works offline, web push (including approvals on a locked phone), and a share target (share a link or file into OpenMuse). |
| **Accounts** | Sign up / sign in. Memory, chats, logins, apps and files are isolated per user. |

## Safety model (short version)

- **The model proposes; policy decides.** Every tool has a risk class from R0 to R5
  (`policies/tool-capabilities.yaml`), and a deterministic policy engine allows it,
  asks you, or denies it.
- **Autonomy is on by default** for reversible local steps. External writes, purchases,
  credential fills, shell commands and anything R3 or higher always ask. Voice can approve
  only R0–R2; anything riskier must be confirmed on screen.
- **Approvals are bound to the exact action.** They are single-use and tied to the
  argument hash; if the action changes, the approval is void.
- **Web pages, emails and tool output are untrusted data**, never instructions,
  checked by taint tracking and injection filters. Web tools can't reach localhost or
  your local network.
- **Secrets never reach the model.** Saved passwords are Fernet-encrypted and filled
  straight into the page, only on the matching site. Connector tokens stay with Composio.

More detail is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#safety).

## Quick start

```bash
git clone https://github.com/0sparsh2/OpenMuse.git && cd OpenMuse
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env        # then add your NVIDIA_NIM_API_KEY (from build.nvidia.com)

python serve_nim.py                                                                 # API on :8765
python -m client.serve_ui --api http://127.0.0.1:8765 --port 8080 --data .data/ui   # UI on :8080
```

Open http://127.0.0.1:8080 and create an account. Optional features turn on when
you add their keys: Composio for Gmail and Calendar, Jev for faster decisions in search
and the browser. See [docs/SETUP.md](docs/SETUP.md) for every setting.

## Documentation

| Doc | For |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | Installing, configuring (every `.env` setting), running, optional features, troubleshooting |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it fits together: turn engine, tools and policy, memory, browser, API, UI, safety, and the original 10 build phases |
| [docs/WEB_SEARCH.md](docs/WEB_SEARCH.md) | How web search works (router, engines, Jev decisions, citations) and Jev in the browser |
| `/openapi.json` on the API server | The external HTTP API (sessions, messages, SSE events, approvals, …) |

## Tests

Each area has a self-contained test script that prints `N/N checks passed` and
exits 0. Most run offline with scripted models. A few also drive real
Chromium, and some add live checks when keys are present in `.env`.

```bash
for f in demo*.py; do python "$f" >/dev/null 2>&1 && echo "ok   $f" || echo "FAIL $f"; done
```

| Suite | Checks | Covers |
|---|---:|---|
| `demo.py` | — | Core turn loop against the mock model |
| `demo_memory.py` | 35 | Layered memory, recall, forgetting |
| `demo_subagents.py` | 24 | Sub-agents, budgets, capability ceilings |
| `demo_browser.py` | 31 | Browser operator, commit barrier, challenges |
| `demo_scheduler.py` | 49 | Schedules, hooks, DST, delivery |
| `demo_connectors.py` | 54 | Connector registry, OAuth, vault |
| `demo_api.py` | 36 | External API, SSE, approvals, idempotency |
| `demo_production.py` | 72 | Queues, quotas, backup/restore, deletion |
| `demo_client.py` | 102 | The web client against the real backend |
| `demo_safety.py` | 43 | Injection, taint, red-team release gate |
| `demo_durability.py` | 16 | Persistence across restarts |
| `demo_isolation.py` | 18 | Per-user isolation |
| `demo_composio.py` | 30 | Gmail/Calendar bridge, injection fixture |
| `demo_monitors.py` | 21 | Price/text/change monitors |
| `demo_library.py` | 22 | Documents, PDF forms |
| `demo_proactive.py` | 23 | Goals, ideas, feed |
| `demo_parallel.py` | 10 | Parallel helpers |
| `demo_logins.py` | 15 | Saved-login vault, downloads |
| `demo_pwa.py` | 36 | PWA, web push crypto, share target, phone layout |
| `demo_voice.py` | 33 | Voice mode; live Riva + NIM latency when keyed |
| `demo_mail.py` | 24 | Attachments, new-mail alerts, free-time card |
| `demo_search.py` | 68 | Web search, network guard, router, citations, sources, UI |
| `demo_cu_jev.py` | 16 | Jev in the browser (fake and real Jev) |
| `demo_stream.py` | 12 | Streaming answers: chunking, streamed tool calls, retries (dropped stream, error inside a stream, all-thinking reply), the growing bubble |

## Project layout

```
agent/        turn engine (state machine), context builder, run models
api/          HTTP API, backend service layer, accounts, schedules, monitors,
              library, proactive, push, voice, mail, parallel helpers
browser/      live Chromium operator (streaming, take control, logins, downloads)
client/       UI server (static app + /v1 proxy), web app (client/web), Python client
connectors/   Composio bridge (Gmail, Calendar), connector registry, vault, OAuth
gateway/      model protocol, router, OpenAI-compatible provider (NIM), streaming sinks
memory/       per-user layered memory service, embeddings, knowledge bank
policy/ policies/   risk classes, policy engine, approvals, autonomy
search/       web search: engines, network guard, extraction, Jev client, router
safety/       taint, injection classifier, strong auth, secret scan, red-team gate
scheduler/ production/ subagents/ storage/ tools/   the rest of the platform
prompts/      versioned prompts (main agent, tool guidance, memory, …)
serve_nim.py  the launcher: wires everything to NIM and starts the API
```

## Roadmap

Done: the Muse-style shell and cards, notifications, task control, accounts
and memory, Composio apps, monitors, goals and ideas, parallel helpers, the
document library, saved logins, PWA and push, voice, web search, and Jev decisions.
Open: real-account verification for Gmail and Calendar (#8, #9), more connectors (#10),
a sandboxed Docker computer (#15), Telegram (#18), and payments, which are
deprioritised (#20). See [#21](https://github.com/0sparsh2/OpenMuse/issues/21).

OpenMuse is an independent project and isn't affiliated with Meta. It uses its
own original name, logo and design.
