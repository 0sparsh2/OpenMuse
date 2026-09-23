"""
Durable turn engine.

Implements the blueprint's turn algorithm as an explicit state machine:

  RECEIVED -> ASSEMBLING_CONTEXT -> AWAITING_MODEL -> EVALUATING_ACTIONS
      -> [WAITING_FOR_APPROVAL] -> EXECUTING_TOOLS -> INGESTING_RESULTS
      -> (loop) -> FINALIZING -> COMPLETED / FAILED / CANCELLED

Every state change is appended to the run's event log as an immutable
envelope. Tool results are ingested as UNTRUSTED data blocks. Deterministic
policy — not the model — is the final authority on every tool call.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from gateway.protocol import Block, ChatMessage, ProviderError
from gateway.router import Router
from observability.events import EventLog
from policy.approvals import ApprovalDecider, ApprovalService, PendingApproval
from policy.engine import ASK, DENY, PolicyEngine, PolicyInput
from tools.executor import (
    ExecutionContext,
    execute_batch,
    prevalidate,
    rejection_envelope,
)
from tools.redaction import looks_like_secret
from tools.registry import ToolRegistry

from .context_builder import ContextBuilder
from .models import (
    ASSEMBLING_CONTEXT,
    AWAITING_MODEL,
    CANCELLED,
    COMPLETED,
    EVALUATING_ACTIONS,
    EXECUTING_TOOLS,
    FAILED,
    FINALIZING,
    INGESTING_RESULTS,
    PAUSED,
    RECEIVED,
    WAITING_FOR_APPROVAL,
    Run,
)


@dataclass
class Deps:
    gateway: Router
    registry: ToolRegistry
    policy: PolicyEngine
    approvals: ApprovalService
    decider: ApprovalDecider
    context_builder: ContextBuilder
    workspace_root: str
    memory_root: Optional[str] = None  # per-user memory store for memory.* tools
    user_id: str = ""                  # the run's user, passed to tool contexts


def _transition(run: Run, log: EventLog, new_state: str) -> None:
    old = run.state
    run.transition(new_state)
    log.append("run.transition", {"from": old, "to": new_state})


def _fail(run: Run, log: EventLog, code: str, message: str) -> Run:
    run.failure_code = code
    run.failure_message = message
    log.append("run.failed", {"code": code, "message": message})
    _transition(run, log, FAILED)
    return run


def _summarize_args(arguments: dict) -> dict:
    """Human-readable, secret-free argument summary for approval cards."""
    summary = {}
    for key, value in arguments.items():
        if isinstance(value, str):
            if looks_like_secret(value):
                summary[key] = "[redacted]"
            elif len(value) > 160:
                summary[key] = value[:160] + f"…[{len(value) - 160} more chars]"
            else:
                summary[key] = value
        else:
            summary[key] = value
    return summary


def _ingest_text(envelope: dict) -> str:
    if envelope["status"] == "succeeded":
        mv = envelope["model_view"]
        body = mv if isinstance(mv, str) else json.dumps(mv, ensure_ascii=False)
        notes = []
        if envelope.get("truncated"):
            notes.append("(truncated)")
        if envelope.get("redactions"):
            notes.append("(redacted: " + ", ".join(envelope["redactions"]) + ")")
        note = " " + " ".join(notes) if notes else ""
        return f"[tool {envelope['tool']} succeeded]{note}\n{body}"
    err = envelope.get("error", {})
    return f"[tool {envelope['tool']} failed: {err.get('code')}] {err.get('safe_message')}"


def advance_run(run: Run, deps: Deps, log: EventLog) -> Run:
    """Drive one run to a terminal state. Synchronous Phase 1 implementation."""
    response = None

    while True:
        # -- budgets & cancellation -------------------------------------
        if run.cancel_requested:
            log.append("run.cancelled", {})
            _transition(run, log, CANCELLED)
            return run
        if run.wall_elapsed_s > run.budgets.max_wall_seconds:
            return _fail(run, log, "WALL_BUDGET", "Run exceeded its wall-clock budget.")
        # Pause only at a step boundary — never mid-tool — so nothing is half done.
        if run.pause_requested and run.state in (RECEIVED, INGESTING_RESULTS):
            log.append("run.paused", {"step": run.step})
            _transition(run, log, PAUSED)
            return run

        # -- model step ----------------------------------------------------
        # WAITING_FOR_APPROVAL re-entry means parked approvals were resolved
        # out of band; rebuild context and let the model re-propose. Any
        # still-valid bound grants are picked up by policy without re-asking.
        if run.state in (RECEIVED, INGESTING_RESULTS, WAITING_FOR_APPROVAL, PAUSED):
            if run.model_calls_used >= run.budgets.max_model_calls:
                return _fail(run, log, "MAX_MODEL_CALLS", "Run exceeded its model-call budget.")
            _transition(run, log, ASSEMBLING_CONTEXT)
            built = deps.context_builder.build(run)
            log.append("context.assembled", built.manifest)
            _transition(run, log, AWAITING_MODEL)
            try:
                response = deps.gateway.complete(
                    built.request,
                    idempotency_key=f"{run.run_id}:{run.step}:model",
                )
            except ProviderError as exc:
                return _fail(run, log, f"PROVIDER_{exc.code}", str(exc))
            log.append("model.response", {
                "tool_calls": [{"name": tc.name, "id": tc.id} for tc in response.tool_calls],
                "stop_reason": response.stop_reason,
                "usage": response.usage,
            })
            run.messages.append(ChatMessage(
                role="assistant",
                blocks=[Block(kind="text", text=response.text)] if response.text else [],
                tool_calls=list(response.tool_calls),
            ))
            run.model_calls_used += 1

        assert response is not None, "response must be set before action evaluation"

        # -- final answer --------------------------------------------------
        if not response.tool_calls:
            _transition(run, log, FINALIZING)
            final = response.text.strip()
            if not final:
                return _fail(run, log, "EMPTY_FINAL", "Model returned no text and no tool calls.")
            run.final_text = final
            log.append("run.completed", {"final_chars": len(final)})
            _transition(run, log, COMPLETED)
            return run

        # -- evaluate proposed actions -------------------------------------
        _transition(run, log, EVALUATING_ACTIONS)
        prevalidated = [
            prevalidate(deps.registry, tc.id, tc.name, tc.arguments)
            for tc in response.tool_calls
        ]
        rejected = [p for p in prevalidated if p.error]
        valid = [p for p in prevalidated if not p.error]

        evaluations: list[tuple] = []  # (prevalidated, decision, grant)
        for p in valid:
            grant = deps.approvals.find_valid_grant(
                run_id=run.run_id, tool_name=p.tool_name,
                tool_version=p.tool.version, argument_hash=p.argument_hash,
            )
            decision = deps.policy.evaluate(PolicyInput(
                tool_name=p.tool_name, tool_version=p.tool.version,
                argument_hash=p.argument_hash,
                risk=deps.policy.risk_of(p.tool_name),
                capabilities=list(p.tool.capabilities),
                side_effect=deps.policy.side_effect_of(p.tool_name),
                argument_summary=_summarize_args(p.arguments),
                has_valid_approval=grant is not None,
            ))
            evaluations.append((p, decision, grant))

        log.append("policy.decisions", {
            "decisions": [
                {"call_id": p.call_id, "tool": p.tool_name,
                 "decision": d.decision, "reason": d.reason_code}
                for p, d, _ in evaluations
            ] + [
                {"call_id": p.call_id, "tool": p.tool_name,
                 "decision": DENY, "reason": p.error["code"]}
                for p in rejected
            ],
        })

        denied = [(p, d) for p, d, _ in evaluations if d.decision == DENY]
        asked = [(p, d) for p, d, _ in evaluations if d.decision == ASK]
        allowed = [(p, g) for p, d, g in evaluations if d.decision == "ALLOW"]

        feedback_lines = [
            f"- {p.tool_name}: {d.reason_code} — {d.safe_explanation}"
            for p, d in denied
        ]
        feedback_lines += [
            f"- {p.tool_name}: {p.error['code']} — {p.error['safe_message']}"
            for p in rejected
        ]
        if feedback_lines:
            run.messages.append(ChatMessage(role="user", blocks=[Block(
                kind="text",
                text="[Policy notice — runtime, trusted. These calls will not run. "
                     "Adjust your plan or explain the limitation to the user.]\n"
                     + "\n".join(feedback_lines),
            )]))
            # If NOTHING is runnable, loop back for a revised plan.
            if not asked and not allowed:
                _transition(run, log, INGESTING_RESULTS)
                run.step += 1
                response = None
                continue

        # -- approvals -------------------------------------------------------
        if asked:
            _transition(run, log, WAITING_FOR_APPROVAL)
            newly_allowed: list[tuple] = []
            approval_failed = False
            for p, decision in asked:
                req = deps.approvals.create_request(
                    run_id=run.run_id, tenant_id=run.tenant_id,
                    tool_name=p.tool_name, tool_version=p.tool.version,
                    argument_hash=p.argument_hash,
                    bind_fields={**_summarize_args(p.arguments),
                                 **(p.tool.approval_bind_fields(p.arguments)
                                    if getattr(p.tool, "approval_bind_fields", None) else {})},
                    risk=deps.policy.risk_of(p.tool_name),
                    template=decision.approval_template,
                )
                log.append("approval.requested", {
                    "approval_id": req.id, "tool": p.tool_name,
                    "risk": req.risk, "bind_fields": req.bind_fields,
                })
                try:
                    verdict = deps.decider.decide(req)
                except PendingApproval:
                    # Parked: a client resolves the request out of band (Phase 1: stays here).
                    log.append("approval.parked", {"approval_id": req.id})
                    return run
                grant = deps.approvals.resolve(req.id, verdict, decided_by=type(deps.decider).__name__)
                log.append("approval.decided", {"approval_id": req.id, "verdict": verdict})
                if grant is None:
                    feedback_lines.append(
                        f"- {p.tool_name}: APPROVAL_DENIED — the user declined this action.")
                    approval_failed = True
                else:
                    newly_allowed.append((p, grant))
            if approval_failed and not newly_allowed and not allowed:
                run.messages.append(ChatMessage(role="user", blocks=[Block(
                    kind="text",
                    text="[Policy notice — runtime, trusted.]\n" + "\n".join(feedback_lines),
                )]))
                _transition(run, log, INGESTING_RESULTS)
                run.step += 1
                response = None
                continue
            allowed.extend(newly_allowed)
            _transition(run, log, EVALUATING_ACTIONS)

        # -- execute ---------------------------------------------------------
        if run.tool_calls_used + len(allowed) > run.budgets.max_tool_calls:
            return _fail(run, log, "MAX_TOOL_CALLS", "Run exceeded its tool-call budget.")
        _transition(run, log, EXECUTING_TOOLS)
        envelopes: list[dict] = []
        for p, grant in allowed:
            exec_ctx = ExecutionContext(
                run_id=run.run_id, tenant_id=run.tenant_id,
                workspace_root=deps.workspace_root, event_log=log,
                approval_grant_id=grant.id if grant else "",
                memory_root=deps.memory_root,
                user_id=deps.user_id or run.user_id,
                # tool timeouts are the tool's own, bounded by the run's remaining time
                deadline_ms=max(30_000, int((run.budgets.max_wall_seconds - run.wall_elapsed_s) * 1000)),
            )
            # consume after execution: the tool re-checks the unused, bound
            # grant itself (browser commits/credential fills), then it's spent
            envelopes.extend(execute_batch(deps.registry, exec_ctx, [p]))
            if grant:
                deps.approvals.consume(grant)
        for p in rejected:
            envelopes.append(rejection_envelope(p))
            log.append("tool.rejected", {"call_id": p.call_id, "tool": p.tool_name,
                                        "code": p.error["code"]})

        # -- ingest results as untrusted data --------------------------------
        for env in envelopes:
            run.messages.append(ChatMessage(
                role="tool", name=env["tool"], tool_call_id=env["call_id"],
                blocks=[Block(kind="data", text=_ingest_text(env),
                              source=f"tool:{env['tool']}", source_ref=env["call_id"],
                              trust="untrusted", sensitivity="personal")],
            ))
        run.tool_calls_used += len(envelopes)
        log.append("tools.executed", {
            "calls": [{"call_id": e["call_id"], "tool": e["tool"], "status": e["status"]}
                      for e in envelopes],
        })
        _transition(run, log, INGESTING_RESULTS)
        run.step += 1
        response = None
