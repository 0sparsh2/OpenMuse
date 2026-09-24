"""
Jev in computer use — real Chromium against local pages.

Offline with a fake decider (deterministic), then — when JEV_API_KEY is in
.env — the same pages against the real Jev API.
"""
from __future__ import annotations

import http.server
import os
import re
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
ENV = {}
if os.path.exists(os.path.join(ROOT, ".env")):
    for line in open(os.path.join(ROOT, ".env")):
        m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
        if m:
            ENV[m.group(1)] = m.group(2)

from browser.live_operator import LiveBrowserOperator  # noqa: E402

RESULTS: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


PAGES = {
    "/article": "<title>Kyoto cherry blossom forecast</title><h1>Kyoto cherry blossom forecast 2027</h1>"
                "<p>Peak bloom in Kyoto is expected from March 28 to April 5, 2027, with Maruyama Park and the "
                "Philosopher's Path usually blooming first.</p><a href='/article'>Related</a>",
    "/cookies": "<title>News</title><div role=dialog><p>We value your privacy. We and our 842 partners use cookies "
                "to personalise ads.</p><button>Accept all</button><button>Reject all</button></div><p>Story…</p>",
    "/login": "<title>Sign in</title><h2>Sign in to continue reading</h2><p>This content is for members.</p>"
              "<input aria-label=Email><input type=password aria-label=Password><button>Sign in</button>",
    "/checkout": "<title>Review your trip</title><h2>Step 3 of 3: Review and pay</h2><p>Total: $412.80, charged to "
                 "Visa ending 4242. Non-refundable fare.</p><button>Continue</button><a href='/article'>Back</a>",
    "/puzzle": "<title>Just a moment</title><p>Please slide the puzzle piece into place so we know it's really you.</p>"
               "<div id=puzzle style='width:300px;height:150px;background:#ddd'></div>",
    "/results": "<title>Search results</title><ul><li><a href='/article'>Kyoto forecast</a></li>"
                "<li><a href='/article'>Tokyo forecast</a></li></ul><button>Next page</button>",
}


class Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path.split("?")[0], "<title>404</title>not found").encode()
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


class FakeDecider:
    """Keyword stand-in for Jev with the same interface."""
    available = True

    def __init__(self):
        self.calls = 0

    def decide(self, state, questions, timeout=None):
        self.calls += 1
        t = (state.get("page_text", "") + " " + state.get("title", "")).lower()
        kind = ("cookie_consent" if "cookies" in t else "login" if "sign in to continue" in t else
                "checkout" if "review and pay" in t else "bot_check" if "puzzle" in t else
                "results" if "search results" in t else "content")
        out = {"kind": {"choice": kind, "probabilities": {kind: 0.92}}}
        if "done" in questions:
            out["done"] = {"noul": 0.9 if ("peak bloom" in t and "kyoto" in state.get("user_request", "").lower()) else 0.1}
        return out

    def noul(self, state, question, timeout=None):
        self.calls += 1
        return 0.9 if ("pay" in state.get("page_text", "").lower() and state.get("button") == "Continue") else 0.05


def ids(obs):
    return {e.split('"')[1]: e.split()[0] for e in obs["interactive_elements"] if '"' in e}


def run_pages(op, base, label):
    sid = op.start_session("t", user_id="u")
    op.goal_for = lambda s: "When will cherry blossoms peak in Kyoto next spring?"
    go = lambda p: op.act(sid, {"kind": "navigate", "url": base + p})["observation"]
    o = go("/cookies")
    check(f"{label}: cookie banner recognised and the hint says dismiss it (prefer reject)",
          o.get("page", {}).get("kind") == "cookie_consent" and any("Reject all" in h for h in o.get("hints", [])), str(o.get("page")))
    o = go("/login")
    check(f"{label}: sign-in wall recognised, with the saved-login hint",
          o.get("page", {}).get("kind") == "login" and any("browser.logins" in h for h in o.get("hints", [])), str(o.get("page")))
    o = go("/results")
    check(f"{label}: results page recognised, no false alarms", o.get("page", {}).get("kind") == "results"
          and not o.get("hints"), str(o))
    o = go("/article")
    check(f"{label}: the page that answers the request is flagged as done",
          o.get("page", {}).get("task_done", 0) >= 0.8 and any("report back" in h for h in o.get("hints", [])), str(o.get("page")))
    o = go("/checkout")
    env = op.act(sid, {"kind": "click", "element_id": ids(o)["Continue"]})
    check(f"{label}: a 'Continue' that pays is held for approval (the keyword check alone missed it)",
          env["status"] == "commit_proposed", env.get("status"))
    o = op.act(sid, {"kind": "navigate", "url": base + "/puzzle"})
    check(f"{label}: a disguised human check pauses for the user", o["status"] == "challenge_paused"
          and op.session_info(sid)["state"] == "challenged", o["status"])
    op.mark_challenge_resolved(sid)
    op.close_session(sid)


def main() -> int:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    op = LiveBrowserOperator(os.path.join(tempfile.mkdtemp(prefix="om-cu-"), "b"))
    try:
        # -- without a decider: behaves exactly as before ------------------------------------
        sid = op.start_session("t", user_id="u")
        o = op.act(sid, {"kind": "navigate", "url": base + "/checkout"})["observation"]
        check("without Jev: no page info added", "page" not in o and "hints" not in o)
        env = op.act(sid, {"kind": "click", "element_id": ids(o)["Continue"]})
        check("without Jev: 'Continue' isn't caught by keywords (why the second opinion helps)",
              env["status"] != "commit_proposed", env["status"])
        o = op.act(sid, {"kind": "navigate", "url": base + "/results"})["observation"]
        for _ in range(3):
            last = op.act(sid, {"kind": "click", "element_id": ids(o)["Next page"]})
            o = last["observation"]
        check("repeating the same step 3 times gets a 'try something else' hint (rules, no Jev needed)",
              any("3 times" in h for h in last["observation"].get("hints", [])), str(last["observation"].get("hints")))
        op.close_session(sid)

        # -- fake decider ------------------------------------------------------------------------
        fake = FakeDecider()
        op.decider = fake
        run_pages(op, base, "fake Jev")
        before = fake.calls
        sid = op.start_session("t", user_id="u")
        op.act(sid, {"kind": "navigate", "url": base + "/article"})
        op.act(sid, {"kind": "scroll", "direction": "down"})
        check("the page check runs once per page, not on every step", fake.calls - before == 1, str(fake.calls - before))
        op.close_session(sid)

        # -- real Jev -----------------------------------------------------------------------------
        if ENV.get("JEV_API_KEY"):
            from search.jev import Jev
            op.decider = Jev(ENV["JEV_API_KEY"])
            run_pages(op, base, "real Jev")
        else:
            print("   (no JEV_API_KEY in .env — skipping real-Jev checks)")
    finally:
        op.shutdown()
        srv.shutdown()
    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
