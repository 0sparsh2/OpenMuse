"""
Saved logins + browser downloads checks (issue #16) — real Chromium against a
local site (login form, a download, and a look-alike origin on another port).
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.environ.pop("NVIDIA_NIM_API_KEY", None)

from api import ApiBackend                              # noqa: E402
from browser.live_operator import LiveBrowserOperator  # noqa: E402
from browser.operator import BrowserError               # noqa: E402
from gateway import ModelResponse, ToolCall             # noqa: E402
from policy import AutonomousDecider                    # noqa: E402

RESULTS: list[tuple[str, bool]] = []
PASSWORD = "Tr0ub4dor&3-correct"
PDF_OK = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF"
PDF_EVIL = b"%PDF-1.4\n1 0 obj << /OpenAction << /S /JavaScript /JS (app.alert(1)) >> >> endobj\n%%EOF"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


class Site(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ctype="text/html", extra=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode())

    def do_GET(self):
        if self.path == "/report.pdf":
            return self._send(PDF_OK, "application/pdf", {"Content-Disposition": 'attachment; filename="report.pdf"'})
        if self.path == "/evil.pdf":
            return self._send(PDF_EVIL, "application/pdf", {"Content-Disposition": 'attachment; filename="invoice.pdf"'})
        self._send("""<title>Sign in</title><form method=post action=/login>
          <input name=user aria-label=Username><input name=pw type=password aria-label=Password>
          <button>Sign in</button></form><a href=/report.pdf>Download report</a> <a href=/evil.pdf>Invoice</a>""")

    def do_POST(self):
        data = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        ok = data.get("pw", [""])[0] == PASSWORD
        self._send(f"<title>Account</title><h1>{'Welcome ' + data.get('user', ['?'])[0] if ok else 'Wrong password'}</h1>")

    def log_message(self, *a):
        pass


def serve():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="openmuse-logins-")
    site, url = serve()
    lookalike, bad_url = serve()          # same page, different origin (port)
    op = LiveBrowserOperator(os.path.join(tmp, "browser"))
    SCRIPT: list = []

    def respond(request, history):
        tools = [m for m in request.messages if m.role == "tool"]
        if len(tools) < len(SCRIPT):
            step = SCRIPT[len(tools)]
            name, args = step(tools) if callable(step) else step
            return ModelResponse(text="", stop_reason="tool_calls",
                                 tool_calls=[ToolCall(id=f"s{len(tools)}", name=name, arguments=args)])
        return ModelResponse(text="Signed in.", stop_reason="stop")

    backend = ApiBackend(workspace_root=os.path.join(tmp, "ws"), respond=respond, browser_operator=op,
                         db_path=os.path.join(tmp, "db.sqlite"), library_root=os.path.join(tmp, "users"),
                         logins_key_file=os.path.join(tmp, "vault.key"))
    backend.decider = AutonomousDecider()
    vault = backend.logins
    try:
        login = vault.add("usr_a", site=url, username="alex", password=PASSWORD)
        ref = login["ref"]
        check("login stored; listing shows no secret",
              "secret" not in login and PASSWORD not in json.dumps(vault.list("usr_a")))
        raw = backend.db.kv_get("logins", login["login_id"])
        check("password encrypted at rest", PASSWORD not in json.dumps(raw) and raw["secret"].startswith("gAAAA"))
        check("key file is private (0600)", oct(os.stat(os.path.join(tmp, "vault.key")).st_mode)[-3:] == "600")

        # -- operator-level guards ---------------------------------------------------
        sid = op.start_session("t", user_id="usr_a")
        obs = op.act(sid, {"kind": "navigate", "url": url})["observation"]
        el = {e.split('"')[1]: e.split()[0] for e in obs["interactive_elements"] if '"' in e}
        try:
            op.act(sid, {"kind": "type", "element_id": el["Username"], "text_ref": ref + "#username"}); ok = False
        except BrowserError as e:
            ok = e.code == "CREDENTIAL_FILL_NEEDS_APPROVAL"
        check("no fill without an approval", ok)
        try:
            op.act(sid, {"kind": "type", "element_id": el["Password"], "text": PASSWORD}); ok = False
        except BrowserError as e:
            ok = e.code == "CREDENTIAL_FIELD"
        check("typing a password as plain text is refused", ok)

        # -- the full agent path: list refs -> fill (parks) -> approve -> signed in --
        def fill(field, submit=False):
            def step(tools):
                ob = json.loads(tools[-1].blocks[0].text.split("\n", 1)[1])
                obs_ = ob.get("observation", ob)
                ids = {e.split('"')[1]: e.split()[0] for e in obs_["interactive_elements"] if '"' in e}
                return ("browser.act", {"session_id": SID[0], "action": {
                    "kind": "type", "element_id": ids["Username" if field == "username" else "Password"],
                    "text_ref": REF[0] + "#" + field, "submit": submit}})
            return step
        SID, REF = [None], [ref]
        SCRIPT[:] = [("browser.logins", {}), ("browser.start_session", {}),
                     lambda tools: ("browser.act", {"session_id": _sid(tools), "action": {"kind": "navigate", "url": url}}),
                     fill("username"), fill("password", submit=True)]

        def _sid(tools):
            SID[0] = json.loads(tools[-1].blocks[0].text.split("\n", 1)[1])["session_id"]
            return SID[0]
        chat = backend.create_session(user_id="usr_a").chat_id
        run, _, _ = backend.submit_message(chat_id=chat, user_id="usr_a",
                                           content=[{"type": "text", "text": "sign me in"}])
        approvals = 0
        t0 = time.time()
        while run.state not in ("COMPLETED", "FAILED") and time.time() - t0 < 90:
            if run.state == "WAITING_FOR_APPROVAL":
                req = backend.approvals.requests[backend._pending_approval[run.run_id]]
                if approvals == 0:
                    check("approval card names the site and account",
                          req.bind_fields.get("credential_site") == url and req.bind_fields.get("credential_user") == "alex")
                approvals += 1
                backend.decide_approval(req.id, decision="approve", argument_hash=req.argument_hash, decided_by="test")
                time.sleep(0.3)
            time.sleep(0.1)
        check("signing in asked the user (once per field)", approvals == 2, str(approvals))
        if SID[0]:
            op.observe(SID[0])
        info = op.session_info(SID[0]) if SID[0] else {}
        check("signed in with the saved login", run.state == "COMPLETED" and info.get("title") == "Account", f"{run.state} {info.get('title')} {info.get('url')}")
        blob = json.dumps([e.payload for e in backend._logs[run.run_id].events], default=str) + \
            json.dumps([e.data for e in backend.eventbus.read_since(run.run_id, -1)]) + \
            " ".join(b.text for m in run.messages for b in m.blocks) + json.dumps(info.get("log"))
        check("password never appears in events, SSE, model context or the activity log", PASSWORD not in blob)
        check("activity log says what happened without the value",
              any(e["label"] == "Entering your saved password" for e in info.get("log", [])))

        # -- origin binding + other users --------------------------------------------
        s2 = op.start_session("t", user_id="usr_a")
        o2 = op.act(s2, {"kind": "navigate", "url": bad_url})["observation"]
        e2 = {e.split('"')[1]: e.split()[0] for e in o2["interactive_elements"] if '"' in e}
        try:
            op.act(s2, {"kind": "type", "element_id": e2["Password"], "text_ref": ref + "#password",
                        "approved_credential_fill": True}); ok = False
        except BrowserError as e:
            ok = e.code == "CREDENTIAL_ORIGIN_MISMATCH"
        check("a look-alike origin can't receive the password", ok)
        s3 = op.start_session("t", user_id="usr_b")
        o3 = op.act(s3, {"kind": "navigate", "url": url})["observation"]
        e3 = {e.split('"')[1]: e.split()[0] for e in o3["interactive_elements"] if '"' in e}
        try:
            op.act(s3, {"kind": "type", "element_id": e3["Password"], "text_ref": ref + "#password",
                        "approved_credential_fill": True}); ok = False
        except BrowserError as e:
            ok = e.code == "CREDENTIAL_UNAVAILABLE"
        check("another user can't use A's login", ok)

        # -- downloads ----------------------------------------------------------------
        s4 = op.start_session("t", user_id="usr_a")
        o4 = op.act(s4, {"kind": "navigate", "url": url})["observation"]
        e4 = {e.split('"')[1]: e.split()[0] for e in o4["interactive_elements"] if '"' in e}
        op.act(s4, {"kind": "click", "element_id": e4["Download report"]})
        time.sleep(1.0)
        o4 = op.observe(s4)
        e4 = {e.split('"')[1]: e.split()[0] for e in o4.interactive_elements if '"' in e}
        op.act(s4, {"kind": "click", "element_id": e4["Invoice"]})
        time.sleep(1.0)
        docs = backend.library.list("usr_a")
        check("download saved to the user's Library", any(d["name"] == "report.pdf" and d["source"] == "browser download"
                                                          for d in docs), str([d["name"] for d in docs]))
        check("a malicious PDF download is blocked", not any(d["name"] == "invoice.pdf" for d in docs)
              and any("blocked" in e["label"] for e in op.session_info(s4)["log"]))
        check("downloads are per user", backend.library.list("usr_b") == [])
    finally:
        op.shutdown()
        site.shutdown(); lookalike.shutdown()

    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
