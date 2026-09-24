"""
Streaming answers — a fake OpenAI-compatible server that really streams
(slow chunks, streamed tool calls, a dropped connection), the real provider
and backend, and real Chromium watching the answer grow.
"""
from __future__ import annotations

import http.server
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.environ.pop("NVIDIA_NIM_API_KEY", None)
os.environ.pop("JEV_API_KEY", None)

from api import ApiBackend                                   # noqa: E402
from api.server import serve                                 # noqa: E402
from client.serve_ui import serve_ui                         # noqa: E402
from gateway.protocol import ProviderError                   # noqa: E402
from gateway.providers.openai_compat import OpenAICompatProvider  # noqa: E402
from policy import AutonomousDecider                         # noqa: E402

RESULTS: list[tuple[str, bool]] = []
ANSWER = ("India's squad for the West Indies ODIs is led by Shubman Gill, with KL Rahul as vice-captain. "
          "Rohit Sharma and Virat Kohli return, and Auqib Nabi and Naman Dhir get their first call-ups. "
          "The three-match series starts on 27 September.")
STATE = {"drop_next": False, "calls": 0}


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


class FakeNim(http.server.BaseHTTPRequestHandler):
    """Streams like NIM: SSE chat.completion.chunk lines, then [DONE]."""
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        STATE["calls"] += 1
        tools_done = any(m.get("role") == "tool" for m in body["messages"])
        wants_tool = "what time" in json.dumps(body["messages"][-3:]).lower() and not tools_done
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def send(obj):
            data = f"data: {json.dumps(obj) if not isinstance(obj, str) else obj}\n\n".encode()
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        def chunk(delta, finish=None):
            send({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
        if wants_tool:   # a tool call arrives in pieces, as real streams do
            chunk({"role": "assistant", "content": "Let me check the clock. "})
            chunk({"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "system__clock", "arguments": ""}}]})
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]})
            chunk({}, "tool_calls")
        else:
            words = ANSWER.split(" ")
            for i, w in enumerate(words):
                if STATE["drop_next"] and i == 8:          # connection drops mid-answer
                    STATE["drop_next"] = False
                    self.wfile.write(b"0\r\n\r\n")
                    self.close_connection = True
                    return
                chunk({"content": w + (" " if i < len(words) - 1 else "")})
                time.sleep(0.06)
            chunk({}, "stop")
        send("[DONE]")
        self.wfile.write(b"0\r\n\r\n")

    def log_message(self, *a):
        pass


def main() -> int:
    nim = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeNim)
    threading.Thread(target=nim.serve_forever, daemon=True).start()
    prov = OpenAICompatProvider(api_key="test", base_url=f"http://127.0.0.1:{nim.server_address[1]}/v1", model="fake")

    def respond(request, history):   # same retry shape as serve_nim.py
        for attempt in range(3):
            try:
                return prov.complete(request)
            except ProviderError as exc:
                if not exc.retryable:
                    raise
        raise RuntimeError("gave up")

    tmp = tempfile.mkdtemp(prefix="om-stream-")
    backend = ApiBackend(workspace_root=os.path.join(tmp, "ws"), respond=respond, db_path=os.path.join(tmp, "db.sqlite"),
                         accounts_root=os.path.join(tmp, "acct"))
    backend.decider = AutonomousDecider()

    def ask(text, uid="usr_a"):
        chat = backend.create_session(user_id=uid).chat_id
        run, _, _ = backend.submit_message(chat_id=chat, user_id=uid, content=[{"type": "text", "text": text}])
        seen, t0 = [], time.time()
        while run.state not in ("COMPLETED", "FAILED") and time.time() - t0 < 30:
            seen.append(len([e for e in backend.eventbus.read_since(run.run_id, -1) if e.type == "assistant.partial"]))
            time.sleep(0.05)
        return run, seen

    # -- 1. a plain answer streams ----------------------------------------------------------
    run, seen = ask("who is in india's odi squad?")
    evts = backend.eventbus.read_since(run.run_id, -1)
    parts = [e.data for e in evts if e.type == "assistant.partial" and "text" in e.data]
    streamed = "".join(p["text"] for p in parts)
    check("the answer arrives in pieces while it's being written", run.state == "COMPLETED" and len(parts) >= 3
          and max(seen) >= 2, f"{len(parts)} parts")
    check("pieces add up to exactly the answer", streamed == ANSWER, repr(streamed[:80]))
    check("tokens are coalesced (fewer events than words)", len(parts) < len(ANSWER.split()),
          f"{len(parts)} events for {len(ANSWER.split())} words")
    final = "".join(e.data.get("text", "") for e in evts if e.type == "assistant.delta")
    check("the finished answer still arrives whole at the end", final == ANSWER)
    db = sqlite3.connect(os.path.join(tmp, "db.sqlite"))
    kinds = {t for (t,) in db.execute("select type from sse_events where run_id=?", (run.run_id,))}
    check("streamed pieces aren't stored one by one (the finished answer is)",
          "assistant.partial" not in kinds and "assistant.delta" in kinds, str(kinds))

    # -- 2. text before a tool call, and a tool call that arrives in pieces ---------------------
    run, _ = ask("what time is it?")
    evts = backend.eventbus.read_since(run.run_id, -1)
    calls = [e.data for e in evts if e.type == "tool.call"]
    steps = sorted({e.data["step"] for e in evts if e.type == "assistant.partial"})
    check("a streamed tool call is assembled and runs", calls and calls[0]["tool"] == "system.clock"
          and run.state == "COMPLETED", str(calls))
    check("each step streams separately (pre-tool text, then the answer)", len(steps) >= 2, str(steps))

    # -- 3. the connection drops mid-answer: the retry replaces, never duplicates ---------------
    STATE["drop_next"] = True
    run, _ = ask("who is in the squad, again?")
    evts = backend.eventbus.read_since(run.run_id, -1)
    resets = [e for e in evts if e.type == "assistant.partial" and e.data.get("reset")]
    after_last_reset = "".join(e.data.get("text", "") for e in evts[evts.index(resets[-1]):]
                               if e.type == "assistant.partial") if resets else ""
    check("a dropped stream is retried and the half-written text is reset", run.state == "COMPLETED"
          and len(resets) >= 2 and after_last_reset == ANSWER, f"{len(resets)} resets")

    # -- 4. real Chromium: the bubble grows before the answer is finished --------------------------
    api_srv = serve(backend)
    api = f"http://127.0.0.1:{api_srv.server_address[1]}"
    req = urllib.request.Request(api + "/v1/auth/signup", method="POST", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"email": "st@example.com", "password": "correct horse 42", "name": "S"}).encode())
    token = json.loads(urllib.request.urlopen(req).read())["token"]
    ui = serve_ui(api, domains_root=os.path.join(tmp, "ui"), require_auth=True)
    base = f"http://localhost:{ui.server_address[1]}"
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context()
        ctx.add_init_script(f"try {{ localStorage.setItem('om.token', {json.dumps(token)}); }} catch (e) {{}}")
        page = ctx.new_page()
        page.goto(base + "/")
        page.fill("textarea[aria-label='Message']", "who is in india's odi squad?")
        page.keyboard.press("Enter")
        page.wait_for_selector(".mc-bubble.streaming", timeout=15000)
        early = page.inner_text(".mc-bubble.streaming")
        page.wait_for_timeout(700)
        later = page.inner_text(".mc-bubble.streaming") if page.query_selector(".mc-bubble.streaming") else ""
        check("the app shows the answer growing, before it's finished",
              0 < len(early) < len(ANSWER) and (len(later) > len(early) or not later), f"{len(early)} -> {len(later)}")
        page.wait_for_selector(".mc-bubble.bot:not(.streaming)", timeout=20000)
        page.wait_for_function("!document.querySelector('.mc-bubble.streaming')", timeout=20000)
        check("when it's done, the finished answer replaces the live text", page.inner_text(".mc-bubble.bot").strip() == ANSWER)
        browser.close()
    nim.shutdown()

    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
