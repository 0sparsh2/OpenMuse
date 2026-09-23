"""Local launcher: API backend on NVIDIA NIM (OpenAI-compatible) + prints an admin key.

Reads NVIDIA_NIM_API_KEY / NVIDIA_NIM_API_BASE / NVIDIA_MODEL from .env.
Then run the UI:  python3 -m client.serve_ui --api http://127.0.0.1:8765 --port 8080 --data <dir>
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

with open(os.path.join(ROOT, ".env")) as f:
    for line in f:
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.strip().split("=", 1)
            os.environ.setdefault(k, v)

from agent.models import RunBudgets
from api import ApiBackend, serve
from browser.live_operator import LiveBrowserOperator
from gateway.providers.openai_compat import OpenAICompatProvider

provider = OpenAICompatProvider(
    api_key=os.environ["NVIDIA_NIM_API_KEY"],
    base_url=os.environ.get("NVIDIA_NIM_API_BASE", "https://integrate.api.nvidia.com/v1"),
    model=os.environ["NVIDIA_MODEL"],
)

port = int(os.environ.get("OPENMUSE_API_PORT", "8765"))
# NIM's hosted endpoints return sporadic 5xx/429s. Retry with backoff, then
# fall back to a second model so one bad minute doesn't fail the whole task.
fallback = OpenAICompatProvider(
    api_key=os.environ["NVIDIA_NIM_API_KEY"],
    base_url=os.environ.get("NVIDIA_NIM_API_BASE", "https://integrate.api.nvidia.com/v1"),
    model=os.environ.get("NVIDIA_FALLBACK_MODEL", "openai/gpt-oss-20b"),
)


def _complete_with_fallback(request, primary=None):
    from gateway.protocol import ProviderError
    last = None
    for prov, attempts in ((primary or provider, 4), (fallback, 3)):
        for attempt in range(attempts):
            try:
                resp = prov.complete(request)
                if prov is fallback:
                    print(f"answered by fallback model {prov.model}", flush=True)
                return resp
            except ProviderError as exc:
                last = exc
                if not exc.retryable:
                    break  # auth/invalid request: the same model won't do better
                wait = min(2 ** attempt, 8)
                print(f"{prov.model}: {exc.code}; retrying in {wait}s", flush=True)
                time.sleep(wait)
    raise last


def _respond(request, history):
    resp = _complete_with_fallback(request)
    if os.environ.get("OPENMUSE_DEBUG"):
        for tc in resp.tool_calls:
            print("TOOL_CALL", tc.name, json.dumps(tc.arguments)[:400], flush=True)
    return resp


# Background memory jobs don't need chain-of-thought: with thinking off the
# extractor is ~2x faster and never burns its token budget before the JSON.
memory_provider = OpenAICompatProvider(
    api_key=os.environ["NVIDIA_NIM_API_KEY"],
    base_url=os.environ.get("NVIDIA_NIM_API_BASE", "https://integrate.api.nvidia.com/v1"),
    model=os.environ.get("NVIDIA_MEMORY_MODEL", os.environ["NVIDIA_MODEL"]),
    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
)


def _llm_json(system: str, user: str) -> str:
    """Background memory jobs (extract / consolidate / compact): one plain
    completion, no tools, deterministic-ish, through the same retry/fallback."""
    from gateway.protocol import Block, ChatMessage, ModelRequest
    req = ModelRequest(
        request_id=f"memory:{time.time_ns()}", model_class="planner",
        messages=[ChatMessage(role="system", blocks=[Block(kind="text", text=system)]),
                  ChatMessage(role="user", blocks=[Block(kind="text", text=user)])],
        tools=[], max_output_tokens=4000, temperature=0.0, metadata=None)
    return _complete_with_fallback(req, primary=memory_provider).text


data_root = os.environ.get("OPENMUSE_DATA", os.path.join(ROOT, ".data", "api"))
os.makedirs(data_root, exist_ok=True)
from memory.service import MemoryService


def _connectors():
    """Composio-backed app connectors when COMPOSIO_API_KEY is set, else None."""
    if not os.environ.get("COMPOSIO_API_KEY"):
        return None
    from connectors.composio_bridge import ComposioBridge
    return ComposioBridge(os.environ["COMPOSIO_API_KEY"],
                          cache_dir=os.path.join(ROOT, ".data", "cache"),
                          public_url=os.environ.get("OPENMUSE_PUBLIC_URL", "http://127.0.0.1:8080"),
                          log=lambda m: print(m, flush=True),
                          timezone_for=lambda uid: backend.user_timezone(uid))

state_root = os.path.join(ROOT, ".data")
memory_service = MemoryService(state_root, prompts_dir=os.path.join(ROOT, "prompts"),
                               llm=_llm_json, log=lambda m: print(m, flush=True))
browser = LiveBrowserOperator(os.path.join(ROOT, ".data", "browser-profile"),
                              headless=os.environ.get("OPENMUSE_HEADFUL") != "1")
backend = ApiBackend(
    workspace_root=data_root,
    respond=_respond,
    browser_operator=browser,
    # browsing takes many steps; each approval-gated step costs a re-proposal
    run_budgets=RunBudgets(max_model_calls=60, max_tool_calls=80, max_wall_seconds=1800),
    memory_service=memory_service,              # per-user layered memory
    accounts_root=os.path.join(state_root, "accounts"),  # sign up / sign in
    db_path=os.path.join(state_root, "openmuse.db"),     # chats/runs/events/approvals survive restarts
    scheduling_root=os.path.join(state_root, "users"),   # per-user schedules + runner
    connectors=_connectors(),                             # Gmail / Calendar via Composio
    enable_monitors=True, monitors_llm=_llm_json,         # price / text / change watches
    library_root=os.path.join(state_root, "users"),      # per-user documents & PDF forms
    enable_proactive=True, proactive_llm=_llm_json,      # goals, ideas (+ generator), feed
    enable_subagents=True,                                # helper agents, incl. parallel fan-out
    logins_key_file=os.path.join(state_root, "vault.key"),  # saved logins (encrypted at rest)
)
backend.proactive.start()
backend.monitors.start()
backend.schedules.start()
# Autonomy (on by default; OPENMUSE_AUTONOMY=off restores ask-for-everything):
# reversible local steps such as browsing run on their own, while commits,
# credentials, shell, external writes and R3+ still wait for approval.
if os.environ.get("OPENMUSE_AUTONOMY", "on").lower() not in ("off", "0", "false"):
    from policy import AutonomousDecider
    backend.decider = AutonomousDecider()

# Keep the UI key stable across restarts: mint once, persist to .env, rebind on start.
# local UI polls the live browser view, so it gets a higher request budget
rec, key = backend.keys.create_key(name="ui", scopes={"admin"}, tenant_id=backend.tenant_id,
                                   rate_limit_per_min=3000)
fixed = os.environ.get("OPENMUSE_API_KEY")
if fixed:
    import dataclasses
    from api.models import hash_key
    store = backend.keys
    del store._by_hash[rec.key_hash]
    rec = dataclasses.replace(rec, key_hash=hash_key(fixed))
    store._by_hash[rec.key_hash] = store._by_id[rec.key_id] = rec
    key = fixed
else:
    with open(os.path.join(ROOT, ".env"), "a") as f:
        f.write(f"OPENMUSE_API_KEY={key}\n")
print(f"model={provider.model} api=http://127.0.0.1:{port}", flush=True)
print("API_KEY=" + key, flush=True)
serve(backend, host="127.0.0.1", port=port)
while True:
    time.sleep(3600)
