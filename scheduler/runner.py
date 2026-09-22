"""Bounded scheduled-run executor — Phase 5.

Runs a job instance in a strictly scoped agent context:

- Tool ceiling: the run sees only tools whose namespace or capabilities are
  in the instance's capability_ceiling (intersection with the registry).
  Anything else is denied with CEILING_DENIED before policy is consulted.
- Policy: the same deterministic R0–R5 engine governs every call. Approvals
  cannot be completed while the user is away, so an ASK decision fails
  closed for this execution (UNATTENDED_FAIL_CLOSED) and is recorded as a
  pending approval the user can resolve later — the run never self-approves.
- The hook/schedule payload is data: instruction text is the task, and for
  hook runs the event payload is marked untrusted.
- Every execution appends a RunRecord (durable run history) and an
  observability event; the delivery critic decides what the user hears.

In production the planned tool calls come from the turn engine running the
instance's instruction snapshot. The demo drives the executor with a
deterministic plan so the policy and delivery behavior can be asserted.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from observability.events import EventLog
from policy import PolicyEngine, PolicyInput, ALLOW, ASK, DENY

from tools import (
    ExecutionContext, ToolRegistry, argument_hash, canonicalize,
    execute_batch, prevalidate,
)

from .delivery import (
    DeliveryDecision, DeliveryInput, DeliveryPolicy, decide as decide_delivery,
    NOTIFY_NOW,
)
from .models import JobInstance, RunRecord
from .store import ScheduleStore


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class UnattendedDecider:
    """Approval decider for background runs: the user is away, so an ASK
    can never be satisfied. It fails closed — the call is denied for this
    execution — and returns a pending-approval record the user can resolve
    later. Nothing is ever auto-approved."""

    def on_ask(self, *, run_id: str, tool_name: str, tool_version: str,
               argument_hash: str, risk: str,
               argument_summary: dict) -> dict:
        return {
            "request_id": "apr_" + uuid.uuid4().hex[:12],
            "run_id": run_id,
            "tool_name": tool_name,
            "tool_version": tool_version,
            "argument_hash": argument_hash,
            "risk": risk,
            "argument_summary": argument_summary,
            "status": "pending",
            "reason": "user is away: approval deferred, call denied for this run",
        }


def ceiling_allows(tool, ceiling: list[str]) -> bool:
    """A tool is in the ceiling if its namespace or any of its capabilities
    is named by the ceiling (blueprint: capability intersection)."""
    namespace = tool.name.split(".", 1)[0]
    names = {namespace, *tool.capabilities}
    return any(entry in names for entry in ceiling)


@dataclass
class RunOutcome:
    record: RunRecord
    envelopes: list = field(default_factory=list)  # tool result envelopes
    scoped_out: list = field(default_factory=list)  # calls denied by the ceiling


class ScheduledRunExecutor:
    def __init__(self, *, store: ScheduleStore, registry: ToolRegistry,
                 policy: PolicyEngine, workspace_root: str,
                 tenant_id: str = "tenant_demo"):
        self.store = store
        self.registry = registry
        self.policy = policy
        self.workspace_root = workspace_root
        self.tenant_id = tenant_id
        self.unattended = UnattendedDecider()

    def execute(self, instance: JobInstance,
                planned_calls: list[dict] | None = None,
                *, now: datetime | None = None) -> RunOutcome:
        """Execute one instance. `planned_calls` = [{tool, arguments}] as
        decided by the (mock) agent turn for the instance's instruction."""
        now = now or datetime.now(timezone.utc)
        run_id = "run_" + uuid.uuid4().hex[:12]
        log = EventLog(run_id)
        log.append("schedule.run_started", {
            "instance_id": instance.instance_id,
            "dedup_key": instance.dedup_key,
            "trigger": instance.trigger,
            "schedule_id": instance.schedule_id,
            "hook_id": instance.hook_id,
            "schedule_version": instance.schedule_version,
            "capability_ceiling": instance.capability_ceiling,
        })
        planned_calls = planned_calls or []
        instance.status = "running"
        self.store.put_instance(instance)

        ctx = ExecutionContext(
            run_id=run_id, tenant_id=self.tenant_id,
            workspace_root=self.workspace_root, event_log=log,
        )
        tool_calls_log: list[dict] = []
        denials: list[dict] = []
        scoped_out: list[dict] = []
        pending_approval: dict | None = None
        failed_closed = False

        for i, planned in enumerate(planned_calls):
            tool_name, arguments = planned["tool"], planned.get("arguments", {})
            try:
                tool = self.registry.get(tool_name)
            except (KeyError, Exception):
                denials.append({"tool": tool_name, "decision": "DENY",
                                "reason": "UNKNOWN_TOOL"})
                continue
            # 1. Capability ceiling — the job's authority is exactly what the
            #    user approved; nothing outside it is even attempted.
            if not ceiling_allows(tool, instance.capability_ceiling):
                scoped_out.append({"tool": tool_name, "reason": "CEILING_DENIED"})
                denials.append({"tool": tool_name, "decision": "DENY",
                                "reason": "CEILING_DENIED"})
                log.append("schedule.tool_ceiling_denied",
                           {"tool": tool_name, "ceiling": instance.capability_ceiling})
                continue
            # 2. Prevalidate: parse -> schema-validate -> canonicalize -> hash.
            call = prevalidate(self.registry, f"call_{i}", tool_name, arguments)
            if call.error:
                denials.append({"tool": tool_name, "decision": "DENY",
                                "reason": call.error["code"]})
                continue
            # 3. Policy. Scheduled runs carry no standing approval; a call
            #    that was pre-approved would arrive with a bound grant.
            decision = self.policy.evaluate(PolicyInput(
                tool_name=tool_name, tool_version=tool.version,
                argument_hash=call.argument_hash, risk=self.policy.risk_of(tool_name),
                capabilities=tool.capabilities, side_effect=tool.side_effect,
                argument_summary={k: str(v)[:80] for k, v in
                                  canonicalize(arguments).items()},
            ))
            arg_summary = {k: str(v)[:80] for k, v in
                           canonicalize(arguments).items()}
            entry = {"tool": tool_name, "arguments": canonicalize(arguments),
                     "decision": decision.decision, "reason": decision.reason_code,
                     "argument_summary": arg_summary}
            if decision.decision == ALLOW:
                envelopes = execute_batch(self.registry, ctx, [call])
                entry["envelope"] = envelopes[0]
                log.append("schedule.tool_executed",
                           {"tool": tool_name, "ok": envelopes[0].get("ok")})
            elif decision.decision == ASK:
                # The user is away: fail closed for this run, defer the
                # approval for the user to resolve later.
                pending_approval = self.unattended.on_ask(
                    run_id=run_id, tool_name=tool_name,
                    tool_version=tool.version,
                    argument_hash=call.argument_hash,
                    risk=self.policy.risk_of(tool_name),
                    argument_summary=entry["argument_summary"])
                entry["decision"] = "DENY"
                entry["reason"] = "UNATTENDED_FAIL_CLOSED"
                entry["pending_approval"] = pending_approval["request_id"]
                denials.append({"tool": tool_name, "decision": "DENY",
                                "reason": "UNATTENDED_FAIL_CLOSED"})
                failed_closed = True
                log.append("schedule.approval_deferred", pending_approval)
            else:
                denials.append({"tool": tool_name, "decision": "DENY",
                                "reason": decision.reason_code})
                log.append("schedule.tool_denied",
                           {"tool": tool_name, "reason": decision.reason_code})
            tool_calls_log.append(entry)

        status = "failed_closed" if failed_closed else "completed"
        if pending_approval and not failed_closed:  # pragma: no cover
            status = "waiting_approval"
        finished = _utcnow()

        # 4. Delivery: what, if anything, does the user hear?
        material = any(e.get("envelope", {}).get("status") == "succeeded"
                       for e in tool_calls_log)
        delivery_in = DeliveryInput(
            material=material,
            result_summary=f"{len(tool_calls_log)} tool calls, "
                           f"{len(denials)} denials",
        )
        dp = DeliveryPolicy.from_dict(instance.delivery_policy or {})
        tz_name = "UTC"
        day = now.date().isoformat()
        decision = decide_delivery(
            delivery_in, dp, now_utc=now, tz_name=tz_name,
            notifications_sent_today=self.store.notifications_today(day))
        if decision.decision == NOTIFY_NOW:
            self.store.record_notification(day)
        log.append("schedule.run_finished",
                   {"status": status,
                    "delivery": decision.decision})

        instance.status = status
        self.store.put_instance(instance)
        record = RunRecord(
            run_id=run_id, instance_id=instance.instance_id,
            dedup_key=instance.dedup_key, trigger=instance.trigger,
            status=status, started_at=now.isoformat(), finished_at=finished,
            schedule_id=instance.schedule_id, hook_id=instance.hook_id,
            tool_calls=tool_calls_log, policy_denials=denials,
            pending_approval=pending_approval,
            delivery={"decision": decision.decision,
                      "rationale": decision.rationale},
            result_summary=delivery_in.result_summary,
        )
        self.store.append_run(record)
        return RunOutcome(record=record,
                          envelopes=[e.get("envelope") for e in tool_calls_log
                                     if "envelope" in e],
                          scoped_out=scoped_out)
