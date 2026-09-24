# OpenMuse setup

How to install, configure and run OpenMuse on your machine, turn on the
optional features, and check that everything works.

## 1. Requirements

- **Python 3.10 or newer.** It's developed on 3.14; nothing needs a newer feature than 3.10.
- **An NVIDIA NIM API key** from [build.nvidia.com](https://build.nvidia.com). The
  same key covers the chat model, embeddings and Riva speech.
- **Chromium for Playwright** (`playwright install chromium`), used by the live
  browser and by the UI tests.
- **Optional:** Node.js, only to syntax-check the web app
  (`node --check client/web/js/ui.js`).

## 2. Install

```bash
git clone https://github.com/0sparsh2/OpenMuse.git
cd OpenMuse
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

Then open `.env` and set at least `NVIDIA_NIM_API_KEY` and `NVIDIA_MODEL`.
`.env` is gitignored, so never commit it.

## 3. Run

Two processes: the API (agent, tools, memory, browser) and the UI server
(static web app plus a proxy to the API).

```bash
# terminal 1 — the API on http://127.0.0.1:8765
python serve_nim.py

# terminal 2 — the web app on http://127.0.0.1:8080
python -m client.serve_ui --api http://127.0.0.1:8765 --port 8080 --data .data/ui
```

Open http://127.0.0.1:8080 and **create an account**. Memory, chats, saved
logins, connected apps and files belong to that account.

On first start, `serve_nim.py` creates an admin API key and appends it to `.env`
as `OPENMUSE_API_KEY`. The key stays the same across restarts, and scripts can
use it (`Authorization: Bearer omk_…`).

Restart `serve_nim.py` after changing backend code or `.env`. UI changes only
need a page reload. The service worker picks up new files on the next load.

### Where data lives

Everything is under `.data/` (gitignored):

| Path | Contents |
|---|---|
| `.data/openmuse.db` | SQLite: chats, runs, events, approvals, notifications, logins, settings |
| `.data/accounts/` | Accounts (scrypt password hashes) and session tokens |
| `.data/users/<user>/` | Per-user memory, documents (Library), schedules |
| `.data/browser-profile/` | Per-user browser profiles (cookies stay per user) |
| `.data/vault.key`, `.data/vapid.json` | Encryption key for saved logins, web-push keys (both `0600`) |
| `.data/serve_nim.out` | Server log, if you start it with `nohup … > .data/serve_nim.out` |

To start completely fresh, stop both servers and delete `.data/`.

## 4. Configuration (`.env`)

Only the first three settings are required. [`.env.example`](../.env.example)
lists every setting with comments.

### Model

| Setting | Default | What it does |
|---|---|---|
| `NVIDIA_NIM_API_KEY` | — (required) | NIM key for the model, embeddings and Riva speech |
| `NVIDIA_MODEL` | — (required) | Main model, e.g. `nvidia/nemotron-3-super-120b-a12b` |
| `NVIDIA_NIM_API_BASE` | `https://integrate.api.nvidia.com/v1` | OpenAI-compatible endpoint. Point it at a self-hosted NIM or any compatible server. |
| `NVIDIA_FALLBACK_MODEL` | `openai/gpt-oss-20b` | Used after repeated 5xx/429 errors from the main model |
| `NVIDIA_MEMORY_MODEL` | `NVIDIA_MODEL` | Background memory extraction, with thinking turned off |
| `NVIDIA_VOICE_MODEL` | `NVIDIA_MODEL` | Voice turns (thinking off, tokens streamed) |
| `NVIDIA_EMBED_MODEL` | `nvidia/nemotron-3-embed-1b` | Memory recall and search passage ranking |

### Optional features

| Feature | Settings | Without it |
|---|---|---|
| **Gmail and Google Calendar** | `COMPOSIO_API_KEY` from [composio.dev](https://composio.dev); `OPENMUSE_PUBLIC_URL` (where OAuth returns, default `http://127.0.0.1:8080`) | The Apps page shows the apps as unavailable |
| **Jev decisions** | `JEV_API_KEY` from TypeSafe AI | Search ranks with embeddings, the router uses rules only, and the browser uses keyword checks only |
| **Voice** | Nothing extra: Riva uses the NIM key. `OPENMUSE_ASR_MODEL` (`whisper` or `parakeet`), `OPENMUSE_TTS_VOICE`. To use other speech servers: `OPENMUSE_STT_BASE/KEY/MODEL`, `OPENMUSE_TTS_BASE/KEY/MODEL`. `OPENMUSE_VOICE=off` disables server speech. | The browser's own speech is used |
| **Web push** | Nothing: VAPID keys are generated into `.data/vapid.json`. To pin them: `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT`. | — |
| **Saved-login encryption key** | `OPENMUSE_VAULT_KEY` (a Fernet key) | Generated into `.data/vault.key` |

### Behaviour

| Setting | Default | What it does |
|---|---|---|
| `OPENMUSE_AUTONOMY` | `on` | `off` asks before every step. On, reversible local steps (reading, browsing, drafts) run on their own; purchases, sends, credential fills, shell and R3+ always ask. |
| `OPENMUSE_HEADFUL` | off | `1` shows the automation browser window on this machine (you can always watch it in the UI) |
| `OPENMUSE_API_PORT` | `8765` | API port |
| `OPENMUSE_DATA` | `.data/api` | Shared workspace root |
| `OPENMUSE_RECALL_MIN_COS` | `0.18` | Minimum similarity for memory recall |
| `OPENMUSE_EMBEDDINGS` | `nim` | Set to anything else to use `OPENAI_API_KEY` embeddings, or the offline deterministic embedder when there's no key |
| `OPENMUSE_DEBUG` | off | `1` prints every tool call the model makes |

In the app, **Apps → Web search → "Search automatically"** controls automatic
search per user. When it's off, OpenMuse searches only when you ask.

## 5. Connecting apps (Gmail, Calendar)

1. Put `COMPOSIO_API_KEY` in `.env` and restart `serve_nim.py`.
2. In the app, open **Apps** and choose **Connect** on Gmail or Google Calendar.
   Google's sign-in opens; approve, and you're sent back to OpenMuse.
3. Reading happens automatically. Sending email and creating, changing or deleting
   events always show an approval card first.
4. Optional: tick **Tell me about new email** on the Gmail card for notifications
   (the inbox is checked every 5 minutes).

## 6. Using it on your phone

The web app is an installable PWA. Browsers only allow service workers and push
on HTTPS (or `localhost`), so expose the UI over HTTPS first, for example:

```bash
cloudflared tunnel --url http://localhost:8080
```

Open the HTTPS URL on your phone and install the app. On iPhone that's
**Share → Add to Home Screen**; open it from the home screen before turning on
notifications. Then choose **Bell → Get notifications on this device**. Set
`OPENMUSE_PUBLIC_URL` to the HTTPS URL if you'll connect apps from the phone.

## 7. Verify

```bash
for f in demo*.py; do python "$f" >/dev/null 2>&1 && echo "ok   $f" || echo "FAIL $f"; done
```

Each suite prints `N/N checks passed` and exits 0 (see the table in the
[README](../README.md#tests)). Most run offline against scripted models in
temporary folders; nothing touches `.data/`. Some also run live checks when keys
are in `.env`:
- `demo_voice.py`: real Riva and NIM, with a latency target under 2.5s. This check
  depends on NVIDIA's hosted services and can miss on a slow day.
- `demo_cu_jev.py`: the real Jev API.

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `FileNotFoundError: .env` on start | `cp .env.example .env` and fill in the NIM settings |
| `provider 5xx` / `TRANSIENT; retrying` in the log | Hosted NIM hiccups. The app retries, then switches to `NVIDIA_FALLBACK_MODEL`. |
| The UI says "Offline" | Is `serve_nim.py` running? Does `--api` point at its port? |
| `401` in the UI | Sign in again, or use a fresh `OPENMUSE_API_KEY` in the key box |
| The browser pauses with "human verification" | Open the live view and complete the check yourself (**Take control**); OpenMuse never solves these |
| Web search returns nothing | DuckDuckGo rate-limits bursts; wait a minute. Unofficial endpoints can also change; update `ddgs`. |
| Apps show "not connected" after OAuth | Check `OPENMUSE_PUBLIC_URL` matches the URL you're using |
| No push notifications | Push needs HTTPS (or localhost), an installed app on iOS, and notification permission |
| Voice is slow to start speaking | Hosted Riva latency varies; the first clause is spoken as soon as it's ready |

## 9. Moving beyond one machine

The code keeps each storage tier behind an interface, so production swaps are
local changes:

| Tier | Here | In production |
|---|---|---|
| API | stdlib HTTP server (`api/server.py`) | ASGI server behind TLS, stateless replicas |
| Database | SQLite (`storage/db.py`) | PostgreSQL (plus pgvector for memory vectors) |
| Queue and workers | in-process threads, `production/queue.py` | Redis or a durable queue with worker pools |
| Browser | one local Chromium (`browser/live_operator.py`) | an isolated browser pool with session affinity |
| Secrets | Fernet key file, Composio-held tokens | a KMS-backed vault |

For the safety design (policy engine, approvals, taint, red-team gate), see
[ARCHITECTURE.md](ARCHITECTURE.md#safety).
