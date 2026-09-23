"""
Composio connector bridge checks (issue #7) — offline, with a fake Composio client.

Proves: curated tools are bridged with trimmed schemas (no model-controlled
user_id), every bridged tool has an explicit risk class, identity is always
the calling user (B can never act on A's Google account), reads run on their
own under autonomy while sends/creates always park for approval, a missing
connection comes back as a clear message, and email results carry a UI card.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from api import ApiBackend                                        # noqa: E402
from connectors.composio_bridge import TOOLKITS, ComposioBridge   # noqa: E402
from gateway import ModelResponse, ToolCall                       # noqa: E402
from policy import AutonomousDecider, PolicyEngine                # noqa: E402

RESULTS: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


class FakeComposio:
    """Just enough of the Composio SDK surface the bridge uses."""

    def __init__(self, connected: dict[str, set]):
        self.connected = connected          # user_id -> {toolkit}
        self.calls: list[tuple[str, str, dict]] = []
        outer = self

        class Tools:
            def get_raw_composio_tools(self, tools=None, **kw):
                return [SimpleNamespace(
                    slug=s, name=s, description=f"{s} does a thing. " + "x" * 900,
                    input_parameters={"type": "object", "title": "Req", "properties": {
                        "user_id": {"type": "string", "default": "me"},
                        "query": {"type": "string", "examples": ["a", "b"], "description": "q" * 500,
                                  "human_parameter_name": "Query"},
                        "recipient_email": {"type": "string"}, "body": {"type": "string"},
                        "subject": {"type": "string"}}, "required": ["user_id"]}) for s in tools]

            def execute(self, slug, arguments, user_id=None, **kw):
                outer.calls.append((slug, user_id, dict(arguments)))
                tk = next(k for k, v in TOOLKITS.items() if slug in v["tools"])
                if tk not in outer.connected.get(user_id, set()):
                    raise Exception(f"404 No connected account found for user ID {user_id} for toolkit {tk}")
                if slug == "GMAIL_FETCH_EMAILS":
                    return {"successful": True, "data": {"messages": [{
                        "sender": "Lincoln Middle School <office@lms.example>", "subject": "Permission slips due Friday",
                        "messageText": "Hi Alex, our class is heading to the aquarium…", "threadId": "t1",
                        "display_url": "https://mail.google.com/mail/u/0/#all/t1"}]}}
                return {"successful": True, "data": {"id": "sent-1"}}

        class Accounts:
            def list(self, user_ids=None, statuses=None, toolkit_slugs=None, **kw):
                items = [SimpleNamespace(id=f"ca_{u}_{t}", status="ACTIVE", toolkit=SimpleNamespace(slug=t))
                         for u in (user_ids or []) for t in outer.connected.get(u, set())]
                return SimpleNamespace(items=items)

            def link(self, user_id, auth_config_id, callback_url=None, **kw):
                return SimpleNamespace(redirect_url=f"https://connect.example/{auth_config_id}?u={user_id}",
                                       id="cr_1")

            def delete(self, acc_id):
                u, t = acc_id[3:].rsplit("_", 1)
                outer.connected.get(u, set()).discard(t)

        class Toolkits:
            def _get_auth_config_id(self, toolkit):
                return f"ac_{toolkit}"

        self.tools, self.connected_accounts, self.toolkits = Tools(), Accounts(), Toolkits()


SCRIPT = {}


def respond(request, history):
    user = next((b.text for m in reversed(request.messages) if m.role == "user"
                 for b in m.blocks if b.trust == "user" and not b.text.startswith("[Runtime")), "")
    tools = [m for m in request.messages if m.role == "tool"]
    plan = SCRIPT.get(user.split()[0], [])
    step = len(tools)
    if step < len(plan):
        name, args = plan[step]
        return ModelResponse(text="", stop_reason="tool_calls",
                             tool_calls=[ToolCall(id=f"c{step}", name=name, arguments=args)])
    return ModelResponse(text="done", stop_reason="stop")


def wait(b, rid, states, t=10):
    t0 = time.time()
    while time.time() - t0 < t:
        if b.runs.get(rid).state in states:
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="openmuse-composio-")
    fake = FakeComposio({"usr_a": {"gmail"}})
    bridge = ComposioBridge("", cache_dir=tmp, client=fake)
    backend = ApiBackend(workspace_root=os.path.join(tmp, "ws"), respond=respond, connectors=bridge)
    backend.decider = AutonomousDecider()

    # -- bridging --------------------------------------------------------------
    tool = backend.registry.get("gmail.fetch_emails")
    props = tool.input_schema.get("properties", {})
    check("curated tools bridged into namespaces",
          all(backend.registry.get(n) for n in ("gmail.send_email", "calendar.create_event")))
    check("model can't choose the Composio user_id", "user_id" not in props
          and "user_id" not in tool.input_schema.get("required", []))
    check("schemas trimmed (no examples / UI hints)", "examples" not in props["query"]
          and "human_parameter_name" not in props["query"] and len(props["query"]["description"]) <= 280)
    check("descriptions capped", len(tool.description) <= 700)
    policy = PolicyEngine(os.path.join(ROOT, "policies", "tool-capabilities.yaml"))
    risks = {n: policy.risk_of(n) for n in ("gmail.fetch_emails", "gmail.create_email_draft",
                                            "gmail.send_email", "gmail.reply_to_thread",
                                            "calendar.events_list", "calendar.create_event",
                                            "calendar.delete_event")}
    check("risk table: reads R1, drafts R2, sends/changes R3",
          risks == {"gmail.fetch_emails": "R1", "gmail.create_email_draft": "R2", "gmail.send_email": "R3",
                    "gmail.reply_to_thread": "R3", "calendar.events_list": "R1",
                    "calendar.create_event": "R3", "calendar.delete_event": "R3"}, str(risks))
    unmapped = [t["name"] for ns in ("gmail", "calendar") for t in backend.registry.load_namespace(ns)
                if policy.risk_of(t["name"]) == "R5"]
    check("every bridged tool has an explicit policy entry", not unmapped, str(unmapped))

    # -- identity + autonomy ------------------------------------------------------
    SCRIPT["read"] = [("gmail.fetch_emails", {"query": "permission slip"})]
    chat_a = backend.create_session(user_id="usr_a").chat_id
    r1, _, _ = backend.submit_message(chat_id=chat_a, user_id="usr_a",
                                      content=[{"type": "text", "text": "read school email"}])
    check("A's read runs on its own under autonomy", wait(backend, r1.run_id, {"COMPLETED"}))
    check("executed as A's Composio user", fake.calls and fake.calls[-1][:2] == ("GMAIL_FETCH_EMAILS", "usr_a"))
    check("safe defaults applied (max_results)", fake.calls[-1][2].get("max_results") == 5)
    cards = [e.data.get("display") for e in backend.eventbus.read_since(r1.run_id, -1)
             if e.type == "tool.result" and e.data.get("display")]
    check("email result carries a UI card", cards and cards[0]["type"] == "email"
          and cards[0]["from"] == "Lincoln Middle School" and cards[0]["subject"].startswith("Permission"))
    check("connected namespace preloaded for A", "gmail" in r1.loaded_namespaces)

    chat_b = backend.create_session(user_id="usr_b").chat_id
    r2, _, _ = backend.submit_message(chat_id=chat_b, user_id="usr_b",
                                      content=[{"type": "text", "text": "read school email"}])
    wait(backend, r2.run_id, {"COMPLETED", "FAILED"})
    check("B's call goes out as B, never as A", fake.calls[-1][1] == "usr_b")
    tool_msgs = [m for m in r2.messages if m.role == "tool"]
    check("B gets a clear 'not connected' message", tool_msgs and "isn't connected" in tool_msgs[-1].blocks[0].text)

    SCRIPT["send"] = [("gmail.send_email", {"recipient_email": "office@lms.example",
                                            "subject": "Re: slip", "body": "Signed tonight."})]
    n_calls = len(fake.calls)
    r3, _, _ = backend.submit_message(chat_id=chat_a, user_id="usr_a",
                                      content=[{"type": "text", "text": "send the reply"}])
    check("sending email parks for approval even with autonomy on",
          wait(backend, r3.run_id, {"WAITING_FOR_APPROVAL"}))
    check("nothing was sent before approval", len(fake.calls) == n_calls)
    req = backend.approvals.requests[backend._pending_approval[r3.run_id]]
    check("approval card shows recipient + subject", req.bind_fields.get("recipient_email") == "office@lms.example"
          and req.bind_fields.get("subject") == "Re: slip")
    backend.decide_approval(req.id, decision="approve", argument_hash=req.argument_hash, decided_by="test")
    check("after Allow, exactly one send as A", wait(backend, r3.run_id, {"COMPLETED"})
          and [c for c in fake.calls[n_calls:] if c[0] == "GMAIL_SEND_EMAIL"] == [
              ("GMAIL_SEND_EMAIL", "usr_a", {"recipient_email": "office@lms.example",
                                             "subject": "Re: slip", "body": "Signed tonight."})])

    # -- connect / disconnect ------------------------------------------------------
    link = bridge.connect("usr_b", "googlecalendar")
    check("connect returns a per-user OAuth link", "u=usr_b" in link["redirect_url"])
    cat = {a["toolkit"]: a for a in bridge.catalog("usr_a")}
    check("catalog shows A connected to Gmail only", cat["gmail"]["connected"] and not cat["googlecalendar"]["connected"])
    bridge.disconnect("usr_a", "gmail")
    check("disconnect removes the connection", not bridge.connected_namespaces("usr_a"))

    # -- not configured ----------------------------------------------------------------
    plain = ApiBackend(workspace_root=os.path.join(tmp, "ws2"))
    check("without a Composio key the app runs with no connectors", plain.apps is None)

    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
