#!/usr/bin/env python3
"""
Phase 1 smoke test: one full agent turn against the mock provider.

Flow:
  1. User: "Write a haiku about personal agents to notes/haiku.txt, then tell me the current time."
  2. Model (mock): loads the `system` and `files` namespaces (deferred discovery).
  3. Model (mock): calls system.clock (R0 -> ALLOW) and files.write (R2 -> ASK -> auto-approved).
  4. Tool results are ingested as untrusted data; model produces the final answer.
  5. Safety checks: secret redaction, unknown-tool rejection, destructive-command
     guard, invalid arguments, duplicate-submission idempotency.

Run:  python3 demo.py
Exit code 0 = all checks passed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from agent import RunStore, ContextBuilder, Deps, advance_run  # noqa: E402
from agent.models import RunBudgets  # noqa: E402
from gateway import Block, ChatMessage, ModelRequest, ModelResponse, Router, ToolCall  # noqa: E402
from gateway.providers.mock import ProgrammableMockProvider  # noqa: E402
from observability import EventLog  # noqa: E402
from policy import PolicyEngine, ApprovalService, AutoApproveDecider  # noqa: E402
from tools import (  # noqa: E402
    ToolRegistry,
    prevalidate,
    redact_text,
)
from tools.namespaces import system_tools, math_tools, file_tools, shell_tools, web_tools, memory_tools  # noqa: E402
from tools.namespaces.shell_tools import is_destructive  # noqa: E402

HAIKU = "Silent agents wake\ntools in hand, they plan and do\nmemory holds the thread\n"


def build_registry(run_loaded: set[str]) -> ToolRegistry:
    registry = ToolRegistry()
    system_tools.register(registry, loaded_namespaces=run_loaded)
    math_tools.register(registry)
    file_tools.register(registry)
    shell_tools.register(registry)
    web_tools.register(registry)
    memory_tools.register(registry)
    return registry


def mock_respond(request: ModelRequest, history: list[ModelRequest]) -> ModelResponse:
    """Deterministic stand-in for the LLM. Reacts to what's in context."""
    tool_names = {t.name for t in request.tools}
    tool_results = [m for m in request.messages if m.role == "tool"]
    result_names = {m.name for m in tool_results}

    if "system.clock" in result_names and "files.write" in result_names:
        # Final step: compose the answer from real tool results.
        clock_iso, written = "unknown", "unknown"
        for m in tool_results:
            body = m.blocks[0].text.split("\n", 1)[-1]
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, IndexError):
                continue
            if m.name == "system.clock":
                clock_iso = data.get("iso", clock_iso)
            if m.name == "files.write":
                written = f"{data.get('path')} ({data.get('bytes_written')} bytes)"
        return ModelResponse(
            text=f"Done. I wrote the haiku to {written}, and the current UTC time is {clock_iso}.",
            stop_reason="stop",
        )

    if "system.clock" in tool_names and "files.write" in tool_names:
        # Namespaces loaded -> do the work.
        return ModelResponse(
            text="",
            tool_calls=[
                ToolCall(id="call_demo_1", name="system.clock", arguments={}),
                ToolCall(id="call_demo_2", name="files.write",
                         arguments={"path": "notes/haiku.txt", "content": HAIKU}),
            ],
            stop_reason="tool_calls",
        )

    # First step: discover capabilities via deferred namespace loading.
    return ModelResponse(
        text="",
        tool_calls=[
            ToolCall(id="call_demo_ns1", name="tools.load_namespace", arguments={"name": "system"}),
            ToolCall(id="call_demo_ns2", name="tools.load_namespace", arguments={"name": "files"}),
        ],
        stop_reason="tool_calls",
    )


def print_transcript(run) -> None:
    print("\n--- transcript -------------------------------------------------")
    for m in run.messages:
        if m.role == "tool":
            body = m.blocks[0].text if m.blocks else ""
            print(f"[tool:{m.name}] {body[:220]}")
        elif m.role == "assistant" and m.tool_calls:
            calls = ", ".join(f"{tc.name}({json.dumps(tc.arguments)[:80]})" for tc in m.tool_calls)
            print(f"[assistant tool_calls] {calls}")
            for b in m.blocks:
                if b.text.strip():
                    print(f"  text: {b.text[:200]}")
        else:
            for b in m.blocks:
                label = f"[{b.kind}/{b.trust}]" if b.kind == "data" else ""
                print(f"[{m.role}]{label} {(b.text[:220]).replace(chr(10), ' ')}")
    print("----------------------------------------------------------------\n")


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    workspace = tempfile.mkdtemp(prefix="muse-replica-demo-ws-")
    user_files = tempfile.mkdtemp(prefix="muse-replica-demo-user-")
    with open(os.path.join(user_files, "USER.md"), "w") as fh:
        fh.write("# USER.md\n\n- Name: Demo User\n- Timezone: America/New_York\n")
    with open(os.path.join(user_files, "MEMORY.md"), "w") as fh:
        fh.write("# MEMORY.md\n\n## Preferences\n- Likes haikus about agents.\n")

    store = RunStore()
    # Submit first so the registry's load_namespace tool shares the run's namespace set.
    run, created = store.submit(
        tenant_id="ten_demo", chat_id="chat_demo", user_id="user_demo",
        user_message=ChatMessage(role="user", blocks=[
            Block(kind="text", text=(
                "Write a haiku about personal agents to notes/haiku.txt, "
                "then tell me the current time."
            ), trust="user"),
        ]),
        idempotency_key="demo-turn-1",
        budgets=RunBudgets(max_model_calls=8, max_tool_calls=10, max_wall_seconds=120),
    )
    assert created

    registry = build_registry(run.loaded_namespaces)
    provider = ProgrammableMockProvider(mock_respond)
    router = Router(routes={"planner": [provider], "fast": [provider]})
    policy = PolicyEngine(os.path.join(ROOT, "policies", "tool-capabilities.yaml"))
    approvals = ApprovalService()
    # DEMO ONLY: auto-approves R1/R2. Production uses ManualDecider + a client UI.
    decider = AutoApproveDecider(allow_risks={"R1", "R2"})
    builder = ContextBuilder(
        prompts_dir=os.path.join(ROOT, "prompts"),
        registry=registry, user_files_dir=user_files,
        agent_name="demo-agent", timezone_name="America/New_York", channel="demo",
    )
    log = EventLog(run.run_id)
    deps = Deps(gateway=router, registry=registry, policy=policy,
                approvals=approvals, decider=decider,
                context_builder=builder, workspace_root=workspace)

    run = advance_run(run, deps, log)
    print_transcript(run)

    ok = True
    ok &= check("run completed", run.state == "COMPLETED", f"state={run.state}")
    ok &= check("final answer mentions haiku file and time",
                "notes/haiku.txt" in run.final_text and "UTC" in run.final_text,
                run.final_text[:100])

    haiku_path = os.path.join(workspace, "notes", "haiku.txt")
    ok &= check("file written inside workspace", os.path.isfile(haiku_path))
    if os.path.isfile(haiku_path):
        with open(haiku_path) as fh:
            ok &= check("file content matches", fh.read() == HAIKU)

    ok &= check("deferred namespaces were loaded", run.loaded_namespaces == {"system", "files"},
                f"loaded={sorted(run.loaded_namespaces)}")
    ok &= check("policy decisions audited",
                any(e.type == "policy.decisions" for e in log.events))
    ok &= check("R2 write went through ASK->approved",
                any(e.type == "approval.decided" and e.payload.get("verdict") == "approved"
                    for e in log.events))
    ok &= check("context manifest recorded",
                any(e.type == "context.assembled" and e.payload.get("content_hash", "").startswith("sha256:")
                    for e in log.events))
    ok &= check("duplicate submission is idempotent",
                store.submit(tenant_id="t", chat_id="c", user_id="u",
                             user_message=ChatMessage(role="user", blocks=[]),
                             idempotency_key="demo-turn-1")[1] is False)

    # -- safety checks ------------------------------------------------------
    redacted, labels = redact_text("connect with api_key=sk-abcdefghijklmnopqrstuvwx now")
    ok &= check("secret redaction", "sk-abcdefghijklmnopqrstuvwx" not in redacted and bool(labels),
                f"labels={labels}")

    bad = prevalidate(registry, "c1", "nope.nope", {})
    ok &= check("unknown tools never reach executors",
                bad.error is not None and bad.error["code"] == "UNKNOWN_TOOL")

    bad_args = prevalidate(registry, "c2", "files.write", {"path": "x.txt"})
    ok &= check("invalid arguments rejected",
                bad_args.error is not None and bad_args.error["code"] == "INVALID_ARGUMENTS")

    ok &= check("destructive shell commands refused deterministically",
                is_destructive("rm -rf /tmp/important") is not None
                and is_destructive("echo hello") is None)

    unmapped_risk = policy.risk_of("evil.tool")
    from policy import PolicyInput
    d = policy.evaluate(PolicyInput(tool_name="evil.tool", tool_version="0",
                                    argument_hash="x", risk=unmapped_risk,
                                    capabilities=[], side_effect="destructive"))
    ok &= check("unmapped tools fail closed (DENY)", d.decision == "DENY")

    print(f"\nevents: {len(log.events)} | workspace: {workspace}")
    print("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
