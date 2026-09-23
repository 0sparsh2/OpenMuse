"""
Parallel subagents checks (issue #13) — offline with a scripted model.
"""
from __future__ import annotations

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
from memory.service import MemoryService                # noqa: E402
from policy import AutonomousDecider                    # noqa: E402

RESULTS: list[tuple[str, bool]] = []
PARENT_PLAN: list = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def respond(request, history):
    system = request.messages[0].blocks[0].text if request.messages and request.messages[0].blocks else ""
    tools = [m for m in request.messages if m.role == "tool"]
    if "focused child agent" in system:  # a helper (subagent-child prompt)
        objective = next(b.text for m in request.messages if m.role == "user" for b in m.blocks)
        slug = objective.split()[1].lower()
        if not tools:
            return ModelResponse(text="", stop_reason="tool_calls", tool_calls=[ToolCall(
                id="c1", name="files.write", arguments={"path": f"notes/{slug}.txt", "content": objective})])
        time.sleep(1.0)  # each child takes ~1s of "work"
        return ModelResponse(text=f"{slug}: best price found", stop_reason="stop")
    if len(tools) < len(PARENT_PLAN):
        name, args = PARENT_PLAN[len(tools)]
        return ModelResponse(text="", stop_reason="tool_calls",
                             tool_calls=[ToolCall(id=f"p{len(tools)}", name=name, arguments=args)])
    return ModelResponse(text="Combined the helpers' results.", stop_reason="stop")


def run_parent(backend, uid, text, plan):
    PARENT_PLAN[:] = plan
    chat = backend.create_session(user_id=uid).chat_id
    run, _, _ = backend.submit_message(chat_id=chat, user_id=uid, content=[{"type": "text", "text": text}])
    t0 = time.time()
    while run.state not in ("COMPLETED", "FAILED") and time.time() - t0 < 30:
        time.sleep(0.05)
    return run, time.time() - t0


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="openmuse-par-")
    from demo_composio import FakeComposio
    mem = MemoryService(tmp, prompts_dir=os.path.join(ROOT, "prompts"), llm=None)
    backend = ApiBackend(workspace_root=os.path.join(tmp, "ws"), respond=respond, memory_service=mem,
                         enable_subagents=True,
                         connectors=ComposioBridge("", cache_dir=tmp, client=FakeComposio({"usr_a": {"gmail"}})))
    backend.decider = AutonomousDecider()

    tasks = ["Check Google Flights for SFO-JFK Dec 14", "Check Kayak for SFO-JFK Dec 14",
             "Check JetBlue for SFO-JFK Dec 14"]
    run, took = run_parent(backend, "usr_a", "compare three sites",
                           [("subagent.parallel", {"tasks": tasks, "capability_ceiling": ["files"]})])
    check("parent completes after the helpers", run.state == "COMPLETED", run.failure_message)
    check("3 helpers ran in parallel (~1s, not ~3s)", took < 2.6, f"{took:.1f}s")
    tool_msg = next(m for m in run.messages if m.role == "tool" and m.name == "subagent.parallel")
    body = tool_msg.blocks[0].text
    check("all helper results returned together",
          all(s in body for s in ("google: best price found", "kayak: best price found", "jetblue: best price found")))
    ws = mem.workspace_root("usr_a")
    written = sorted(os.listdir(os.path.join(ws, "notes"))) if os.path.isdir(os.path.join(ws, "notes")) else []
    check("helpers act in the user's own workspace", written == ["google.txt", "jetblue.txt", "kayak.txt"], str(written))
    check("nothing leaked into the shared workspace", not os.path.exists(os.path.join(tmp, "ws", "notes")))
    evts = [e for e in backend.eventbus.read_since(run.run_id, -1) if e.type == "subagent"]
    running = [e for e in evts if e.data["status"] == "running"]
    done = [e for e in evts if e.data["status"] == "completed"]
    check("UI sees each helper start and finish", len(running) == 3 and len(done) == 3
          and running[0].data["objective"].startswith("Check Google"))
    parent = backend.subagents._parents[run.run_id]
    check("helper budgets were carved from the parent", parent.used["model_calls"] >= 3 * 6)

    # -- capability ceiling: helpers never get external writes -----------------------
    from subagents.runner import DelegationRequest
    did = backend.subagents.spawn(DelegationRequest(
        task="Read my email", allowed_namespaces=["gmail"], budget_carve={}, max_depth=1,
        parent_run_id=run.run_id, output_contract={}, context_refs=[], join_policy="all",
        explicit_writes=[]), defer=True)
    tools = backend.subagents._child_tools[did]
    check("helpers can read email but never send", "gmail.fetch_emails" in tools
          and not any(t in tools for t in ("gmail.send_email", "gmail.reply_to_thread", "gmail.forward_message")),
          str(tools))
    did2 = backend.subagents.spawn(DelegationRequest(
        task="Nest", allowed_namespaces=["subagent", "files"], budget_carve={}, max_depth=1,
        parent_run_id=run.run_id, output_contract={}, context_refs=[], join_policy="all",
        explicit_writes=[]), defer=True)
    check("helpers at max depth can't spawn more helpers",
          not any(t.startswith("subagent.") for t in backend.subagents._child_tools[did2]))

    # -- guard rails on the tool itself ------------------------------------------------
    run2, _ = run_parent(backend, "usr_a", "one task only",
                         [("subagent.parallel", {"tasks": ["Check only Google Flights"]})])
    notices = " ".join(b.text for m in run2.messages for b in m.blocks)
    check("a single task is rejected before anything runs",
          "subagent.parallel" in notices and ("INVALID" in notices or "failed" in notices.lower())
          and not any(r.parent_run_id == run2.run_id for r in backend.subagents._delegations.values()))

    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
