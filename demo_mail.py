"""
Gmail + Calendar extras (issues #8, #9) — offline with a fake Composio:
free-time card, attachments into the Library, and opt-in new-mail alerts.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.environ.pop("NVIDIA_NIM_API_KEY", None)

from api import ApiBackend                              # noqa: E402
from api.server import serve                            # noqa: E402
from client.serve_ui import serve_ui                    # noqa: E402
from connectors.composio_bridge import ComposioBridge, _free_card  # noqa: E402
from gateway import ModelResponse, ToolCall             # noqa: E402
from policy import AutonomousDecider                    # noqa: E402

RESULTS: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


FREE = {"kind": "calendar#freeBusy", "timeMin": "2026-09-24T00:00:00-07:00", "timeMax": "2026-09-26T00:00:00-07:00",
        "calendars": {
            "me@example.com": {"busy": [], "free": [
                {"start": "2026-09-24T00:00:00-07:00", "end": "2026-09-24T09:00:00-07:00"},
                {"start": "2026-09-24T11:00:00-07:00", "end": "2026-09-24T15:00:00-07:00"},
                {"start": "2026-09-24T16:00:00-07:00", "end": "2026-09-25T10:00:00-07:00"},
                {"start": "2026-09-25T13:00:00-07:00", "end": "2026-09-26T00:00:00-07:00"}]},
            "priya@example.com": {"busy": [], "free": [
                {"start": "2026-09-24T00:00:00-07:00", "end": "2026-09-24T12:00:00-07:00"},
                {"start": "2026-09-24T14:00:00-07:00", "end": "2026-09-24T14:20:00-07:00"},
                {"start": "2026-09-24T17:00:00-07:00", "end": "2026-09-26T00:00:00-07:00"}]}}}


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="openmuse-mail-")
    from demo_composio import FakeComposio
    INBOX: list[dict] = []
    ATTACH = {"file": None}

    class Fake(FakeComposio):
        def __init__(self, connected):
            super().__init__(connected)
            base_exec = self.tools.execute
            outer = self

            def execute(slug, arguments, user_id=None, **kw):
                if slug == "GMAIL_GET_ATTACHMENT":
                    outer.calls.append((slug, user_id, dict(arguments)))
                    if "gmail" not in outer.connected.get(user_id, set()):
                        raise Exception("404 No connected account found")
                    return {"successful": True, "data": {"file": ATTACH["file"]}}
                if slug == "GOOGLECALENDAR_FIND_FREE_SLOTS":
                    outer.calls.append((slug, user_id, dict(arguments)))
                    return {"successful": True, "data": FREE}
                if slug == "GMAIL_FETCH_EMAILS" and "is:unread" in arguments.get("query", ""):
                    outer.calls.append((slug, user_id, dict(arguments)))
                    if "gmail" not in outer.connected.get(user_id, set()):
                        raise Exception("404 No connected account found")
                    return {"successful": True, "data": {"messages": list(INBOX)}}
                return base_exec(slug, arguments, user_id=user_id, **kw)
            self.tools.execute = execute

    fake = Fake({"usr_a": {"gmail", "googlecalendar"}})
    SCRIPT: list = []

    def respond(request, history):
        tools = [m for m in request.messages if m.role == "tool"]
        if len(tools) < len(SCRIPT):
            name, args = SCRIPT[len(tools)]
            return ModelResponse(text="", stop_reason="tool_calls",
                                 tool_calls=[ToolCall(id=f"m{len(tools)}", name=name, arguments=args)])
        return ModelResponse(text="Done.", stop_reason="stop")

    backend = ApiBackend(workspace_root=os.path.join(tmp, "ws"), respond=respond, db_path=os.path.join(tmp, "db.sqlite"),
                         library_root=os.path.join(tmp, "users"), accounts_root=os.path.join(tmp, "acct"),
                         enable_proactive=True, proactive_llm=lambda s, u: json.dumps({"ideas": []}),
                         connectors=ComposioBridge("", cache_dir=tmp, client=fake))
    backend.decider = AutonomousDecider()

    def run(uid, plan, text="go"):
        SCRIPT[:] = plan
        chat = backend.create_session(user_id=uid).chat_id
        r, _, _ = backend.submit_message(chat_id=chat, user_id=uid, content=[{"type": "text", "text": text}])
        t0 = time.time()
        while r.state not in ("COMPLETED", "FAILED", "WAITING_FOR_APPROVAL") and time.time() - t0 < 15:
            time.sleep(0.05)
        return r

    def results(r):
        return [e.data for e in backend.eventbus.read_since(r.run_id, -1) if e.type == "tool.result"]

    # -- free-time card (#9) -----------------------------------------------------------------
    card = _free_card({"data": FREE})
    labels = [s["label"] for s in card["slots"]]
    check("free time is what's free for everyone", card["title"] == "Free times for all 2" and card["people"] == 2)
    check("slots are clipped to waking hours and at least 30 min",
          labels == ["Thu Sep 24 · 8 AM–9 AM", "Thu Sep 24 · 11 AM–12 PM", "Thu Sep 24 · 5 PM–8 PM",
                     "Fri Sep 25 · 8 AM–10 AM", "Fri Sep 25 · 1 PM–8 PM"], str(labels))
    check("slot times keep the user's timezone", card["slots"][0]["start"] == "2026-09-24T08:00:00-07:00")
    none = _free_card({"data": {"calendars": {"a": {"free": [{"start": "2026-09-24T21:00:00-07:00",
                                                              "end": "2026-09-24T23:00:00-07:00"}]}}}})
    check("no free waking time says so", none["slots"] == [] and "No free time" in none["title"])
    r = run("usr_a", [("calendar.find_free_slots", {"items": ["me@example.com", "priya@example.com"],
                                                    "time_min": "2026-09-24", "time_max": "2026-09-26"})])
    disp = next((x.get("display") for x in results(r) if x.get("display")), None)
    check("the agent's free-slot search shows the free-time card", disp and disp["type"] == "free_slots"
          and len(disp["slots"]) == 5, str(disp)[:200])

    # -- attachments -> Library (#8) -------------------------------------------------------------
    att = os.path.join(tempfile.gettempdir(), "om-test-slip.pdf")
    with open(att, "wb") as fh:
        fh.write(b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF")
    ATTACH["file"] = att
    r = run("usr_a", [("gmail.save_attachment", {"message_id": "m1", "attachment_id": "ANGjdJ9", "file_name": "slip.pdf"})])
    docs = backend.library.list("usr_a")
    check("an attachment is saved to the Library without asking (R2, local)", r.state == "COMPLETED"
          and any(d["name"] == "slip.pdf" and d["source"] == "gmail" for d in docs), f"{r.state} {[d['name'] for d in docs]}")
    evil = os.path.join(tempfile.gettempdir(), "om-test-evil.pdf")
    with open(evil, "wb") as fh:
        fh.write(b"%PDF-1.4\n1 0 obj << /OpenAction << /S /JavaScript /JS (app.alert(1)) >> >> endobj\n%%EOF")
    ATTACH["file"] = evil
    r = run("usr_a", [("gmail.save_attachment", {"message_id": "m2", "attachment_id": "X", "file_name": "invoice.pdf"})])
    body = " ".join(b.text for m in r.messages if m.role == "tool" for b in m.blocks)
    check("a booby-trapped PDF attachment is refused", not any(d["name"] == "invoice.pdf" for d in backend.library.list("usr_a"))
          and "successful\": false" in body.replace("'", "\""), body[:200])
    ATTACH["file"] = {"name": "x.pdf", "mimetype": "application/pdf", "s3url": "http://evil.example/x.pdf"}
    r = run("usr_a", [("gmail.save_attachment", {"message_id": "m3", "attachment_id": "Y", "file_name": "x.pdf"})])
    check("attachment links must be https", "isn't https" in " ".join(b.text for m in r.messages for b in m.blocks))
    ATTACH["file"] = "/etc/passwd"
    r = run("usr_a", [("gmail.save_attachment", {"message_id": "m4", "attachment_id": "Z", "file_name": "p.txt"})])
    check("a path outside the download folder is refused",
          not any(d["name"] == "p.txt" for d in backend.library.list("usr_a")))
    fake.connected["usr_b"] = set()
    r = run("usr_b", [("gmail.save_attachment", {"message_id": "m1", "attachment_id": "A", "file_name": "slip.pdf"})])
    check("without Gmail connected, it says to connect", backend.library.list("usr_b") == [] and
          "isn't connected" in " ".join(b.text for m in r.messages for b in m.blocks))

    # -- new-mail alerts (#8) -----------------------------------------------------------------
    mw = backend.mailwatch
    INBOX[:] = [{"messageId": "e1", "sender": "Coach Dana <dana@club.example>", "subject": "Practice moved"}]
    check("alerts are off until you turn them on", mw.settings("usr_a")["enabled"] is False and mw.due() == [])
    mw.set_enabled("usr_a", True)
    check("turning them on schedules a check", mw.due() == ["usr_a"])
    sent = mw.check("usr_a")
    check("the first check doesn't flood you with existing mail", sent == 0 and not any(
        n["kind"] == "email" for n in backend.notifications("usr_a")))
    INBOX.insert(0, {"messageId": "e2", "sender": "\"Lincoln Middle School\" <office@lms.example>",
                     "subject": "Field trip form due Friday"})
    sent = mw.check("usr_a")
    notes = [n for n in backend.notifications("usr_a") if n["kind"] == "email"]
    check("new mail sends a notification with sender and subject", sent == 1 and notes
          and notes[0]["title"] == "New email from Lincoln Middle School" and notes[0]["body"] == "Field trip form due Friday")
    check("the same email never notifies twice", mw.check("usr_a") == 0)
    INBOX[:0] = [{"messageId": f"n{i}", "sender": f"s{i}@x.example", "subject": f"s{i}"} for i in range(5)]
    sent = mw.check("usr_a")
    notes = [n for n in backend.notifications("usr_a") if n["kind"] == "email"]
    check("a burst is summarised (3 + “N more”)", sent == 3 and any(n["title"] == "2 more new emails" for n in notes))
    check("checks aren't due again until the interval passes", mw.due() == [])
    mw.set_enabled("usr_b", True)
    mw.check("usr_b")
    check("without Gmail connected the check reports why", mw.settings("usr_b")["last_error"] == "Gmail isn't connected")
    check("other users' alerts are separate", not any(n["kind"] == "email" for n in backend.notifications("usr_b")))

    # -- HTTP + UI -------------------------------------------------------------------------------
    api_srv = serve(backend)
    api = f"http://127.0.0.1:{api_srv.server_address[1]}"

    def call(method, path, body=None, token=""):
        req = urllib.request.Request(api + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    st, res = call("POST", "/v1/auth/signup", {"email": "m@example.com", "password": "correct horse 42", "name": "Mo"})
    token, uid = res["token"], res["user"]["user_id"]
    fake.connected[uid] = {"gmail", "googlecalendar"}
    st, a = call("GET", "/v1/apps/gmail/alerts", token=token)
    st2, b = call("PUT", "/v1/apps/gmail/alerts", {"enabled": True}, token=token)
    check("alerts setting over the API", st == 200 and a["enabled"] is False and st2 == 200 and b["enabled"] is True)

    ui = serve_ui(api, domains_root=os.path.join(tmp, "ui"), require_auth=True)
    base = f"http://localhost:{ui.server_address[1]}"
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context()
        ctx.add_init_script(f"try {{ localStorage.setItem('om.token', {json.dumps(token)}); }} catch (e) {{}}")
        page = ctx.new_page()
        page.goto(base + "/")
        SCRIPT[:] = [("calendar.find_free_slots", {"items": ["me@example.com", "priya@example.com"],
                                                   "time_min": "2026-09-24", "time_max": "2026-09-26"})]
        page.fill("textarea[aria-label='Message']", "When are Priya and I both free?")
        page.keyboard.press("Enter")
        page.wait_for_selector(".mc-slot", timeout=15000)
        chips = page.eval_on_selector_all(".mc-slot", "els => els.map((e) => e.textContent)")
        check("free-time card shows tappable slots", len(chips) == 5 and chips[0] == "Thu Sep 24 · 8 AM–9 AM", str(chips))
        page.wait_for_function("!document.querySelector('.mc-send.stop')", timeout=15000)
        page.click(".mc-slot >> nth=2")
        val = page.input_value("textarea[aria-label='Message']")
        check("tapping a slot starts the booking message", val == "Book Thu Sep 24 · 5 PM–8 PM for ", val)
        page.click("#tabbar button[data-tab='connectors']")
        page.wait_for_selector("input[aria-label='Tell me about new email']", timeout=10000)
        check("Apps shows the new-email toggle for connected Gmail, reflecting the setting",
              page.is_checked("input[aria-label='Tell me about new email']"))
        page.uncheck("input[aria-label='Tell me about new email']")
        page.wait_for_timeout(500)
        check("…and the toggle turns alerts off", backend.mailwatch.settings(uid)["enabled"] is False)
        browser.close()

    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
