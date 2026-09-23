"""
Goals / Ideas / Feed checks (issue #12) — offline with a scripted model.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.environ.pop("NVIDIA_NIM_API_KEY", None)

from api import ApiBackend                              # noqa: E402
from connectors.composio_bridge import ComposioBridge   # noqa: E402
from gateway import ModelResponse, ToolCall             # noqa: E402
from memory.records import MemoryRecord, new_memory_id  # noqa: E402
from memory.service import MemoryService                # noqa: E402
from policy import AutonomousDecider                    # noqa: E402

RESULTS: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="openmuse-pro-")
    mem = MemoryService(tmp, prompts_dir=os.path.join(ROOT, "prompts"), llm=None)
    LLM_OUT = {"text": "{}"}
    SEEN = {}

    def llm(system, user):
        SEEN["ctx"] = json.loads(user)
        return LLM_OUT["text"]

    SCRIPT: list = []

    def respond(request, history):
        tools = [m for m in request.messages if m.role == "tool"]
        if len(tools) < len(SCRIPT):
            name, args = SCRIPT[len(tools)]
            return ModelResponse(text="", stop_reason="tool_calls",
                                 tool_calls=[ToolCall(id=f"g{len(tools)}", name=name, arguments=args)])
        return ModelResponse(text="On it.", stop_reason="stop")

    from demo_composio import FakeComposio
    fake = FakeComposio({"usr_a": {"gmail"}})
    backend = ApiBackend(workspace_root=os.path.join(tmp, "ws"), respond=respond, memory_service=mem,
                         db_path=os.path.join(tmp, "db.sqlite"), enable_proactive=True, proactive_llm=llm,
                         connectors=ComposioBridge("", cache_dir=tmp, client=fake))
    backend.decider = AutonomousDecider()
    pro = backend.proactive

    # -- goals -----------------------------------------------------------------------
    g = pro.create_goal("usr_a", title="Run a half marathon", target_date="2027-03-01",
                        milestones=["Run 5k", "Run 10k", {"title": "Run 15k", "due": "2027-01-15"}])
    check("goal created with milestones", len(g["milestones"]) == 3 and g["milestones"][2]["due"] == "2027-01-15")
    g = pro.update_goal("usr_a", g["goal_id"], complete_milestone="run 5k", note="Felt great")
    check("milestone completed by title", g["milestones"][0]["done"] and g["activity"][-1]["text"] == "Felt great")
    g = pro.update_goal("usr_a", g["goal_id"], add_milestone="Race day")
    check("milestone added", g["milestones"][-1]["title"] == "Race day")
    check("B can't see A's goals", pro.goals("usr_b") == [] and pro._get("goals", "usr_b", g["goal_id"]) is None)

    # -- ideas: propose / dedupe / dismiss / snooze / accept -----------------------------
    i1 = pro.propose("usr_a", title="Book a physio check before training", action_prompt="Find a sports physio near me")
    check("idea proposed", i1 and i1["status"] == "new")
    check("duplicate suggestion suppressed", pro.propose("usr_a", title="book a physio check before training!") is None)
    pro.dismiss("usr_a", i1["idea_id"], reason="already have one")
    check("dismissed idea leaves the list", all(i["idea_id"] != i1["idea_id"] for i in pro.ideas("usr_a")))
    check("dismissed idea never comes back", pro.propose("usr_a", title="Book a physio check before training") is None)
    journal = [e.text for e in mem.memory("usr_a").journal.recent_entries(10)]
    check("dismissal recorded in memory for future ideas", any("dismissed the suggestion" in t for t in journal))
    i2 = pro.propose("usr_a", title="Buy running shoes", action_prompt="Compare 3 beginner running shoes under $120")
    pro.snooze("usr_a", i2["idea_id"], days=2)
    check("snoozed idea hidden", all(i["idea_id"] != i2["idea_id"] for i in pro.ideas("usr_a")))
    real_now = time.time
    import api.proactive as P
    P._now = lambda: real_now() + 3 * 86400
    check("snoozed idea returns after the snooze", any(i["idea_id"] == i2["idea_id"] for i in pro.ideas("usr_a")))
    P._now = real_now
    res = pro.accept("usr_a", i2["idea_id"])
    run = backend.runs.get(res["run_id"])
    t0 = time.time()
    while run.state not in ("COMPLETED", "FAILED") and time.time() - t0 < 10:
        time.sleep(0.05)
    check("accepting starts a chat that runs the action prompt", run.state == "COMPLETED"
          and backend._run_text[run.run_id].startswith("Compare 3 beginner running shoes")
          and backend.sessions[res["chat_id"]].title == "Buy running shoes")

    # -- generator ----------------------------------------------------------------------------
    m = mem.memory("usr_a")
    rec = MemoryRecord(memory_id=new_memory_id(), kind="commitment", claim="User must return Maya's field-trip form by Friday.")
    m.curated.add(rec)
    LLM_OUT["text"] = json.dumps({"ideas": [
        {"title": "Return Maya's field-trip form", "rationale": "Due Friday.", "action_prompt": "Find the form and fill it",
         "evidence": [{"kind": "memory", "ref": rec.memory_id}]},
        {"title": "Book a physio check before training", "rationale": "x", "action_prompt": "x",
         "evidence": [{"kind": "goal", "ref": g["goal_id"]}]},
        {"title": "Invest in crypto", "rationale": "made up", "action_prompt": "buy",
         "evidence": [{"kind": "memory", "ref": "mem_doesnotexist"}]},
        {"title": "Answer the school's email", "rationale": "unread", "action_prompt": "Draft a reply",
         "evidence": [{"kind": "email", "ref": "email:t1"}]}]})
    fake.connected["usr_a"].add("gmail")
    new = pro.generate("usr_a")
    titles = [i["title"] for i in new]
    check("context includes memory, goals and (untrusted) recent email",
          any(x["id"] == rec.memory_id for x in SEEN["ctx"]["memory"]) and SEEN["ctx"]["goals"]
          and SEEN["ctx"].get("recent_email_untrusted"))
    check("context lists dismissed ideas so they aren't re-suggested",
          any("physio" in d for d in SEEN["ctx"]["dismissed"]))
    check("ideas need real evidence (made-up refs dropped)", "Invest in crypto" not in titles)
    check("dismissed ideas filtered even if the model repeats them", "Book a physio check before training" not in titles)
    check("valid ideas kept with labelled evidence", "Return Maya's field-trip form" in titles
          and new[0]["evidence"][0]["label"].startswith("User must return"))
    email_idea = next((i for i in new if i["title"] == "Answer the school's email"), None)
    check("ideas built on email are marked as such", email_idea and email_idea["from_email"])
    check("user notified about new ideas", any(n["kind"] == "ideas" for n in backend.notifications("usr_a")))
    check("generator stays quiet for a user with no context", pro.generate("usr_new") == [])

    # -- agent tools + feed + persistence ---------------------------------------------------
    SCRIPT[:] = [("goals.create", {"title": "Learn Spanish", "milestones": ["100 words", "First conversation"]})]
    chat = backend.create_session(user_id="usr_a").chat_id
    r, _, _ = backend.submit_message(chat_id=chat, user_id="usr_a",
                                     content=[{"type": "text", "text": "I want to learn Spanish"}])
    t0 = time.time()
    while r.state not in ("COMPLETED", "FAILED") and time.time() - t0 < 10:
        time.sleep(0.05)
    cards = [e.data.get("display") for e in backend.eventbus.read_since(r.run_id, -1)
             if e.type == "tool.result" and e.data.get("display")]
    check("agent creates a goal on its own with a goal card", r.state == "COMPLETED" and cards
          and cards[0]["type"] == "goal" and cards[0]["milestones"] == ["100 words", "First conversation"])
    pro.post("usr_a", title="Weekly summary", body="3 tasks done")
    check("feed post visible to its owner only", pro.feed("usr_a")[0]["title"] == "Weekly summary" and pro.feed("usr_b") == [])
    b2 = ApiBackend(workspace_root=os.path.join(tmp, "ws"), db_path=os.path.join(tmp, "db.sqlite"), enable_proactive=True)
    check("goals and ideas survive a restart", len(b2.proactive.goals("usr_a")) == 2
          and len(b2.proactive.ideas("usr_a", status="all")) == len(pro.ideas("usr_a", status="all")))

    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
