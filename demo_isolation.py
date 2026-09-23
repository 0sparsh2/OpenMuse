"""
Cross-user isolation checks (issue #3).

Two users, A and B, sign up through the real UI server (auth required) in
front of the real API. A creates chats, runs, memory, documents, goals and a
browser profile; every attempt by B (or by an anonymous caller) to reach A's
data must fail as "not found" / "unauthorized", and B's browser must never
inherit A's cookies.

Runs offline: scripted mock model, deterministic embedder, a local HTTP page
for the cookie test, and Playwright's bundled Chromium.
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.environ.pop("NVIDIA_NIM_API_KEY", None)   # deterministic embeddings offline
os.environ.pop("OPENAI_API_KEY", None)

from api import ApiBackend, serve                      # noqa: E402
from client.serve_ui import serve_ui                   # noqa: E402
from memory.service import MemoryService               # noqa: E402

RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def call(base, method, path, body=None, token=None, headers=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    h.update(headers or {})
    req = urllib.request.Request(base + path, method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait_run(base, token, run_id):
    for _ in range(100):
        st, r = call(base, "GET", f"/v1/runs/{run_id}", token=token)
        if r.get("state") in ("COMPLETED", "FAILED", "CANCELLED"):
            return r
        time.sleep(0.1)
    return r


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="openmuse-iso-")
    mem = MemoryService(tmp, prompts_dir=os.path.join(ROOT, "prompts"), llm=None)
    backend = ApiBackend(workspace_root=os.path.join(tmp, "ws"), memory_service=mem,
                         accounts_root=os.path.join(tmp, "accounts"))
    api = serve(backend, host="127.0.0.1", port=0)
    api_base = f"http://127.0.0.1:{api.server_address[1]}"
    ui = serve_ui(api_base, host="127.0.0.1", port=0,
                  domains_root=os.path.join(tmp, "ui"), require_auth=True)
    base = f"http://127.0.0.1:{ui.server_address[1]}"

    # -- accounts ------------------------------------------------------------
    st, a = call(base, "POST", "/v1/auth/signup", {"email": "a@example.com", "password": "password-a1", "name": "A"})
    st2, b = call(base, "POST", "/v1/auth/signup", {"email": "b@example.com", "password": "password-b1", "name": "B"})
    check("both users sign up", st == 201 and st2 == 201)
    A, B = a["token"], b["token"]

    # -- A's resources ---------------------------------------------------------
    _, s = call(base, "POST", "/v1/sessions", {"title": "A chat"}, A)
    chat = s["chat_id"]
    _, r = call(base, "POST", f"/v1/chats/{chat}/messages",
                {"content": [{"type": "text", "text": "My home airport is SFO."}]}, A,
                {"Idempotency-Key": "iso-1"})
    run_id = r["run_id"]
    wait_run(base, A, run_id)
    call(base, "POST", "/v1/memory/documents", {"title": "A secret plan", "text": "Surprise party for Jo on Friday."}, A)
    call(base, "POST", "/v1/local/goals", {"title": "A's goal"}, A)
    call(base, "POST", "/v1/local/ideas", {"text": "A's idea"}, A)

    # -- B cannot reach any of it ---------------------------------------------
    check("B cannot read A's session", call(base, "GET", f"/v1/sessions/{chat}", token=B)[0] == 404)
    check("B cannot post into A's chat",
          call(base, "POST", f"/v1/chats/{chat}/messages", {"content": [{"type": "text", "text": "hi"}]}, B,
               {"Idempotency-Key": "iso-2"})[0] == 404)
    check("B cannot read A's run", call(base, "GET", f"/v1/runs/{run_id}", token=B)[0] == 404)
    check("B cannot stream A's run events", call(base, "GET", f"/v1/runs/{run_id}/events", token=B)[0] == 404)
    check("B cannot cancel A's run", call(base, "POST", f"/v1/runs/{run_id}/cancel", {}, B)[0] == 404)
    _, bm = call(base, "GET", "/v1/memory", token=B)
    check("B's memory is empty", bm["stats"]["documents"] == 0 and bm["stats"]["journal_entries"] == 0)
    _, hits = call(base, "POST", "/v1/memory/recall", {"query": "surprise party"}, B)
    check("B's recall never returns A's documents", not hits["results"])
    _, bg = call(base, "GET", "/v1/local/goals", token=B)
    check("B sees none of A's goals", bg == [])
    _, bi = call(base, "GET", "/v1/local/ideas", token=B)
    check("B sees none of A's ideas", bi == [])
    _, ag = call(base, "GET", "/v1/local/goals", token=A)
    check("A still sees their goal", len(ag) == 1 and ag[0]["title"] == "A's goal")
    check("anonymous /v1/local/* is rejected", call(base, "GET", "/v1/local/goals")[0] == 401)
    check("bogus token /v1/local/* is rejected", call(base, "GET", "/v1/local/feed", token="omk_nope")[0] == 401)
    _, am = call(base, "GET", "/v1/memory", token=A)
    check("A's memory has A's document", am["stats"]["documents"] == 1)

    # -- browser profiles: cookies never cross users --------------------------
    try:
        from browser.live_operator import LiveBrowserOperator
    except ImportError:
        LiveBrowserOperator = None
    if LiveBrowserOperator is None:
        print("[SKIP] playwright not installed — browser profile isolation not checked")
    else:
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                if self.path == "/set":
                    self.send_header("Set-Cookie", "who=A; Path=/; Max-Age=3600")
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<title>t</title><p>ok</p>")

            def log_message(self, *a):
                pass
        site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=site.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{site.server_address[1]}"
        op = LiveBrowserOperator(os.path.join(tmp, "browser"))
        try:
            def cookies(sid):
                return op._call(lambda: op._sessions[sid].context.cookies())
            sa = op.start_session("t", user_id="usr_a")
            op.act(sa, {"kind": "navigate", "url": url + "/set"})
            check("A's browser got the cookie", any(c["name"] == "who" for c in cookies(sa)))
            op.close_session(sa)            # persists A's profile
            sb = op.start_session("t", user_id="usr_b")
            op.act(sb, {"kind": "navigate", "url": url + "/"})
            check("B's new browser has none of A's cookies", not any(c["name"] == "who" for c in cookies(sb)))
            sa2 = op.start_session("t", user_id="usr_a")
            check("A's next session restores A's own profile", any(c["name"] == "who" for c in cookies(sa2)))
            check("sessions record their owner", op.session_info(sb)["user_id"] == "usr_b")
        finally:
            op.shutdown()
            site.shutdown()

    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
