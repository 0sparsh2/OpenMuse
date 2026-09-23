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


def _complete_with_fallback(request):
    from gateway.protocol import ProviderError
    last = None
    for prov, attempts in ((provider, 4), (fallback, 3)):
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


data_root = os.environ.get("OPENMUSE_DATA", os.path.join(ROOT, ".data", "api"))
os.makedirs(data_root, exist_ok=True)
browser = LiveBrowserOperator(os.path.join(ROOT, ".data", "browser-profile"),
                              headless=os.environ.get("OPENMUSE_HEADFUL") != "1")
backend = ApiBackend(
    workspace_root=data_root,
    respond=_respond,
    browser_operator=browser,
    # browsing takes many steps; each approval-gated step costs a re-proposal
    run_budgets=RunBudgets(max_model_calls=60, max_tool_calls=80, max_wall_seconds=1800),
)
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
