"""Phase 3 verification: subagents + skills.

Proves, fully offline and deterministic:
  A. spawn -> child runs tools under policy -> typed handoff -> close
  B. approval routing: a child's R2 write is approved through the PARENT's decider
  C. fan-out/fan-in (join all + any) and a pipeline join, with typed synthesis
  D. capability ceiling: a tool outside the ceiling is denied (CEILING_DENIED)
  E. skills: catalog/validation, trigger fixtures, only-selected-text injection
  F. delegation loops blocked: max-depth child loses the subagents namespace;
     spawning past max depth raises
  G. parent cancellation propagates to deferred children
  H. budget carving is never additive
  I. child memory is isolated from the parent's store
  J. send() runs one more refinement pass on remaining budget
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from agent.seams import DelegationRequest
from gateway.providers.mock import ProgrammableMockProvider
from gateway.protocol import ModelResponse, ToolCall
from gateway.router import Router
from observability.events import EventLog
from policy import PolicyEngine, ApprovalService, AutoApproveDecider
from subagents import coordinator
from subagents.coordinator import SpawnSpec
from subagents.models import ChildStatus, JoinPolicy
from subagents.namespace import register as register_subagents
from subagents.runner import DelegationError, ParentRunInfo, SubagentRunner
from subagents.skills import SkillCatalog
from tools import namespaces as ns
from tools.namespaces import (
    file_tools, math_tools, memory_tools, shell_tools, system_tools, web_tools,
)
from tools.registry import ToolRegistry

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


# ---------------------------------------------------------------------------
# Deterministic stand-in for the LLM: dispatches on a task marker in context.
# ---------------------------------------------------------------------------
def respond(request, history):
    texts = " ".join(b.text for m in request.messages for b in m.blocks)
    tool_results = [m for m in request.messages if m.role == "tool"]

    def final(obj):
        return ModelResponse(text=json.dumps(obj), tool_calls=[], stop_reason="stop")

    def call(name, args):
        return ModelResponse(
            text="", tool_calls=[ToolCall(id=f"tc_{len(history)}_{name}",
                                          name=name, arguments=args)],
            stop_reason="tool_calls")

    if "CEILING_DENIED" in texts:
        return final({"answer": "denied-as-expected"})
    if "pineapple" in texts and tool_results:
        return final({"answer": 42, "refined": True})
    if "[TASK:SURVEY]" in texts:
        if not tool_results:
            return call("files.list", {"path": "."})
        return final({"directories": [{"path": ".", "purpose": "demo root"}],
                      "entry_points": ["demo_subagents.py"], "notes": "ok"})
    if "[TASK:COUNT]" in texts:
        if not tool_results:
            return call("math.calc", {"expression": "6*7"})
        return final({"answer": 42})
    if "[TASK:WRITE]" in texts:
        if not tool_results:
            return call("files.write", {"path": "child-note.txt", "content": "hello"})
        return final({"written": True})
    if "[TASK:NAUGHTY]" in texts:
        if not tool_results:
            return call("shell.exec", {"command": "echo hi"})
        return final({"answer": "unexpected"})
    if "[TASK:SPAWNER]" in texts:
        if not tool_results:
            return call("subagent.spawn", {"objective": "nested"})
        return final({"answer": "unexpected"})
    if "[TASK:MEMO]" in texts:
        if not tool_results:
            return call("memory.note", {"note": "The child scratch marker is zebra-42."})
        return final({"stored": True})
    return final({"answer": "fallback"})


def build_runner(workspace_root: str):
    registry = ToolRegistry()
    loaded: set[str] = set()
    system_tools.register(registry, loaded_namespaces=loaded)
    math_tools.register(registry)
    file_tools.register(registry)
    shell_tools.register(registry)
    web_tools.register(registry)
    memory_tools.register(registry)

    router = Router({"fast": [ProgrammableMockProvider(respond)]})
    policy = PolicyEngine(os.path.join(ROOT, "policies", "tool-capabilities.yaml"))
    approvals = ApprovalService()
    decider = AutoApproveDecider(allow_risks={"R1", "R2"})
    runner = SubagentRunner(gateway=router, registry=registry, policy=policy,
                            approvals=approvals, decider=decider,
                            workspace_root=workspace_root, max_depth=2)
    register_subagents(registry, runner)

    parent_log = EventLog("run_parent_demo")
    parent = ParentRunInfo(
        run_id="run_parent_demo", tenant_id="tenant_demo",
        budgets={"model_calls": 200, "tool_calls": 400, "wall_seconds": 3600},
        depth=0, loaded_namespaces=set(registry.namespace_names()),
        event_log=parent_log)
    runner.register_parent(parent)
    return runner, registry, policy, approvals, parent, parent_log


def spawn_req(task, ceiling, contract, budget=None, max_depth=2):
    return DelegationRequest(
        task=task, allowed_namespaces=ceiling,
        budget_carve=budget or {"model_calls": 6, "tool_calls": 12, "wall_seconds": 120},
        max_depth=max_depth, parent_run_id="run_parent_demo",
        output_contract=contract)


COUNT_CONTRACT = {"type": "object", "required": ["answer"],
                  "properties": {"answer": {"type": "integer"}}}


def main() -> int:
    workspace = tempfile.mkdtemp(prefix="openmuse-phase3-")
    runner, registry, policy, approvals, parent, parent_log = build_runner(workspace)

    # -- A. spawn -> tools under policy -> typed handoff -> close ----------
    did = runner.spawn(spawn_req(
        "[TASK:COUNT] compute 6*7 and return it as JSON.",
        ["math"], COUNT_CONTRACT,
        budget={"model_calls": 4, "tool_calls": 4, "wall_seconds": 60}))
    st = runner.status(did)
    check("A1 child completed", st["status"] == ChildStatus.COMPLETED, st["status"])
    check("A2 child ran tools under policy", st["budget_used"]["tool_calls"] >= 1,
          json.dumps(st["budget_used"]))
    check("A3 typed handoff matches contract",
          st["result"]["status"] == "completed" and st["result"]["output"] == {"answer": 42},
          json.dumps(st.get("result")))
    check("A4 parent log recorded lifecycle",
          any(e.type == "subagent.spawned" for e in parent_log.events)
          and any(e.type == "subagent.completed" for e in parent_log.events))
    closed = runner.close(did)
    check("A5 close releases the delegation", closed["closed"] is True)

    # -- H. budget carve is never additive ---------------------------------
    check("H1 budget carved from parent (not additive)",
          parent.used["model_calls"] == 4 and parent.used["tool_calls"] == 4,
          json.dumps(parent.used))

    # -- B. approval routed through the parent's decider --------------------
    did_w = runner.spawn(spawn_req(
        "[TASK:WRITE] write child-note.txt with content 'hello'.",
        ["files"], {"type": "object", "required": ["written"]}))
    st_w = runner.status(did_w)
    check("B1 child R2 write completed via parent approval",
          st_w["status"] == ChildStatus.COMPLETED
          and st_w["result"]["output"] == {"written": True}, st_w["status"])
    check("B2 grant filed on the parent's ApprovalService, decided by parent decider",
          any(r.decided_by == "AutoApproveDecider" for r in approvals.requests.values())
          and os.path.isfile(os.path.join(workspace, "child-note.txt")))

    # -- C. fan-out/fan-in --------------------------------------------------
    ids = coordinator.fan_out(runner, "run_parent_demo", [
        SpawnSpec(task="[TASK:COUNT] compute 6*7.", allowed_namespaces=["math"],
                  output_contract=COUNT_CONTRACT),
        SpawnSpec(task="[TASK:SURVEY] survey the demo root.",
                  allowed_namespaces=["files"],
                  output_contract={"type": "object",
                                   "required": ["directories", "entry_points", "notes"]}),
    ])
    check("C1 fan-out spawned two children", len(ids) == 2)
    syn = coordinator.fan_in(runner, ids, join_policy=JoinPolicy.ALL)
    check("C2 join=all synthesized", syn.status == "synthesized", syn.status)
    check("C3 synthesis is typed and sourced",
          len(syn.synthesis["findings"]) == 2
          and set(syn.synthesis["evidence_refs"]) >= set(ids), json.dumps(syn.notes))
    ids_any = coordinator.fan_out(runner, "run_parent_demo", [
        SpawnSpec(task="[TASK:COUNT] compute 6*7.", allowed_namespaces=["math"],
                  output_contract=COUNT_CONTRACT),
        SpawnSpec(task="[TASK:COUNT] compute 6*7 again.", allowed_namespaces=["math"],
                  output_contract=COUNT_CONTRACT),
    ])
    syn_any = coordinator.fan_in(runner, ids_any, join_policy=JoinPolicy.ANY)
    loser = runner.status(ids_any[1])
    check("C4 join=any picks first valid and closes the rest",
          syn_any.status == "synthesized" and loser["closed"] is True,
          f"{syn_any.status} closed={loser['closed']}")
    pipe = coordinator.pipeline(runner, "run_parent_demo", [
        SpawnSpec(task="[TASK:COUNT] compute 6*7.", allowed_namespaces=["math"],
                  output_contract=COUNT_CONTRACT),
        SpawnSpec(task="[TASK:SURVEY] survey, using the prior artifact.",
                  allowed_namespaces=["files"],
                  output_contract={"type": "object",
                                   "required": ["directories", "entry_points", "notes"]}),
    ])
    check("C5 pipeline join passes typed artifacts stage to stage",
          pipe.status == "synthesized" and pipe.synthesis["stages"] == 2, pipe.status)

    # -- D. capability ceiling ----------------------------------------------
    did_n = runner.spawn(spawn_req(
        "[TASK:NAUGHTY] run a shell command.", ["math"], COUNT_CONTRACT))
    st_n = runner.status(did_n)
    clog = runner._child_logs[did_n]
    check("D1 tool outside the ceiling is denied",
          st_n["result"]["output"] == {"answer": "denied-as-expected"}
          and any(e.type == "child.tool.ceiling_denied" for e in clog.events),
          json.dumps(st_n.get("result")))

    # -- E. skills ------------------------------------------------------------
    catalog = SkillCatalog(os.path.join(ROOT, "skills"))
    check("E1 catalog loads both reference skills",
          catalog.names() == ["memory-hygiene", "workspace-survey"], str(catalog.names()))
    loaded_ns = {"files", "memory"}
    fixtures_ok = True
    for skill_name in catalog.names():
        with open(os.path.join(ROOT, "skills", skill_name, "tests", "trigger.yaml"),
                  encoding="utf-8") as fh:
            cases = yaml.safe_load(fh)["cases"]
        for case in cases:
            selected = [s.name for s in catalog.select(case["query"], loaded_ns)]
            if (skill_name in selected) != case["expect_selected"]:
                fixtures_ok = False
    check("E2 trigger fixtures select the right skills", fixtures_ok)
    text, pinned = catalog.render_context(
        catalog.select("survey the project structure", loaded_ns))
    check("E3 only selected skill text enters context",
          "Workspace Survey" in text and "Memory Hygiene" not in text
          and pinned.get("workspace-survey") == "1.0.0", str(pinned))
    check("E4 required namespaces verified at selection",
          catalog.select("tidy my memories", {"files"}) == [])

    # -- F. delegation loops blocked -------------------------------------------
    did_s = runner.spawn(spawn_req(
        "[TASK:SPAWNER] spawn a nested child.", ["subagents"],
        {"type": "object", "required": ["answer"]}, max_depth=1))
    st_s = runner.status(did_s)
    check("F1 max-depth child loses the subagents namespace",
          st_s["result"]["output"] == {"answer": "denied-as-expected"},
          json.dumps(st_s.get("result")))
    deep_parent = ParentRunInfo(run_id="run_deep", tenant_id="tenant_demo",
                                depth=2, loaded_namespaces=set(registry.namespace_names()))
    runner.register_parent(deep_parent)
    try:
        runner.spawn(DelegationRequest(task="too deep", allowed_namespaces=["math"],
                                       parent_run_id="run_deep"))
        too_deep_ok = False
    except DelegationError:
        too_deep_ok = True
    check("F2 spawning past max depth raises", too_deep_ok)

    # -- G. parent cancellation propagates --------------------------------------
    did_d = runner.spawn(spawn_req("[TASK:COUNT] deferred.", ["math"], COUNT_CONTRACT),
                         defer=True)
    check("G1 deferred child is spawned-not-running",
          runner.status(did_d)["status"] == ChildStatus.SPAWNED)
    cancelled = runner.cancel_all("run_parent_demo")
    check("G2 parent cancellation propagates to live children",
          did_d in cancelled
          and runner.status(did_d)["status"] == ChildStatus.CANCELLED)

    # -- I. child memory isolation ------------------------------------------------
    did_m = runner.spawn(spawn_req(
        "[TASK:MEMO] store the scratch marker.", ["memory"],
        {"type": "object", "required": ["stored"]}))
    st_m = runner.status(did_m)
    parent_mem = os.path.join(workspace, ".agent-memory")
    child_mem = os.path.join(parent_mem, "children", did_m)
    leaked = False
    if os.path.isdir(parent_mem):
        for dp, _, fns in os.walk(parent_mem):
            if "children" in dp:
                continue
            for fn in fns:
                with open(os.path.join(dp, fn), encoding="utf-8", errors="ignore") as fh:
                    if "zebra-42" in fh.read():
                        leaked = True
    child_has = False
    if os.path.isdir(child_mem):
        for dp, _, fns in os.walk(child_mem):
            for fn in fns:
                with open(os.path.join(dp, fn), encoding="utf-8", errors="ignore") as fh:
                    if "zebra-42" in fh.read():
                        child_has = True
    check("I1 child memory lands in scratch, never the parent store",
          st_m["status"] == ChildStatus.COMPLETED and child_has and not leaked)

    # -- J. send() refinement -------------------------------------------------------
    did_j = runner.spawn(spawn_req(
        "[TASK:COUNT] compute 6*7.", ["math"], COUNT_CONTRACT))
    before = runner.status(did_j)["result_versions"]
    after = runner.send(did_j, "also include the word pineapple in your reasoning")
    check("J1 send() runs one more pass on remaining budget",
          after["result_versions"] == before + 1
          and after["result"]["output"] == {"answer": 42, "refined": True},
          json.dumps(after.get("result")))

    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
