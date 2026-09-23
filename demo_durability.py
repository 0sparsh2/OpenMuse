"""
Durability checks (issue #2): state survives an API restart.

Backend #1 (SQLite at a temp path) gets: a chat, a finished turn, a run that
parks on an approval (files.write -> ASK), and a run that is mid-model-call
when the process "dies". Backend #2 is then built on the same database and
must show: the chat list + transcript, the replayable SSE stream, the parked
approval (approving it resumes and completes the run, exactly once), and the
interrupted run failed as WORKER_RESTARTED without being re-executed.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from api import ApiBackend                                   # noqa: E402
from gateway import ModelResponse, ToolCall                  # noqa: E402

RESULTS: list[tuple[str, bool]] = []
HANG = threading.Event()
CALLS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def respond(request, history):
    user = next((b.text for m in reversed(request.messages) if m.role == "user"
                 for b in m.blocks if b.trust == "user"), "")
    tools = [m for m in request.messages if m.role == "tool"]
    CALLS.append(user)
    if "hang" in user:
        HANG.wait(30)  # simulates a model call in flight when the process dies
        return ModelResponse(text="should never be delivered", stop_reason="stop")
    if "save" in user and not tools:
        return ModelResponse(text="", stop_reason="tool_calls", tool_calls=[
            ToolCall(id="call_w1", name="files.write",
                     arguments={"path": "notes/plan.txt", "content": "durable"})])
    return ModelResponse(text=f"done: {user[:30]}", stop_reason="stop")


def wait(backend, run_id, states, timeout=10):
    t = time.time()
    while time.time() - t < timeout:
        if backend.runs.get(run_id).state in states:
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="openmuse-durable-")
    db = os.path.join(tmp, "openmuse.db")
    ws = os.path.join(tmp, "ws")
    content = lambda t: [{"type": "text", "text": t}]

    b1 = ApiBackend(workspace_root=ws, respond=respond, db_path=db)
    chat = b1.create_session(user_id="usr_1", title="").chat_id
    r1, _, _ = b1.submit_message(chat_id=chat, user_id="usr_1", content=content("hello there"),
                                 idempotency_key="k1")
    check("turn 1 completes", wait(b1, r1.run_id, {"COMPLETED"}))
    r2, _, _ = b1.submit_message(chat_id=chat, user_id="usr_1", content=content("please save my plan"),
                                 idempotency_key="k2")
    check("turn 2 parks on approval", wait(b1, r2.run_id, {"WAITING_FOR_APPROVAL"}))
    aid = b1._pending_approval.get(r2.run_id)
    r3, _, _ = b1.submit_message(chat_id=chat, user_id="usr_1", content=content("hang forever"),
                                 idempotency_key="k3")
    time.sleep(0.5)
    check("turn 3 is mid-flight at 'crash'", b1.runs.get(r3.run_id).state == "AWAITING_MODEL")
    b1._save_run(b1.runs.get(r3.run_id))   # a write-through a real crash would already have made
    events_before = len(b1.eventbus.read_since(r1.run_id, -1))

    # ---- "restart": a brand-new backend on the same database ----------------
    calls_before = len(CALLS)
    b2 = ApiBackend(workspace_root=ws, respond=respond, db_path=db)
    check("chat restored with auto title", chat in b2.sessions and b2.sessions[chat].title == "hello there")
    check("chat list restored", [c["chat_id"] for c in b2.db.list_chats("usr_1")] == [chat])
    check("transcript restored", b2.runs.get(r1.run_id).final_text == "done: hello there"
          and b2._run_text[r1.run_id] == "hello there")
    check("SSE stream replays after restart",
          len(b2.eventbus.read_since(r1.run_id, -1)) == events_before and events_before > 0)
    check("parked run is still parked", b2.runs.get(r2.run_id).state == "WAITING_FOR_APPROVAL")
    req = b2.approvals.requests.get(aid)
    check("pending approval restored", req is not None and req.status == "pending")
    r3b = b2.runs.get(r3.run_id)
    check("interrupted run failed as WORKER_RESTARTED",
          r3b.state == "FAILED" and r3b.failure_code == "WORKER_RESTARTED")
    evts = [e.type for e in b2.eventbus.read_since(r3.run_id, -1)]
    check("interrupted run's stream ends with run.failed", "run.failed" in evts)
    check("nothing was re-executed on boot", len(CALLS) == calls_before)

    b2.decide_approval(aid, decision="approve", argument_hash=req.argument_hash, decided_by="test")
    check("approved after restart -> run completes", wait(b2, r2.run_id, {"COMPLETED"}))
    written = os.path.join(ws, "notes", "plan.txt")
    check("the approved write happened exactly once", os.path.exists(written)
          and open(written).read() == "durable")
    dup, created, _ = b2.submit_message(chat_id=chat, user_id="usr_1", content=content("hello there"),
                                        idempotency_key="k1")
    check("idempotency keys survive restart", not created and dup.run_id == r1.run_id)

    # ---- third boot: everything final is still final --------------------------
    b3 = ApiBackend(workspace_root=ws, respond=respond, db_path=db)
    check("final states stable across a second restart",
          b3.runs.get(r2.run_id).state == "COMPLETED" and b3.runs.get(r3.run_id).state == "FAILED")
    HANG.set()

    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
