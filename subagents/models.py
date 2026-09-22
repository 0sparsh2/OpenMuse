"""Subagent data model — mirrors the blueprint's delegation record.

A delegation is the durable, auditable unit of parent -> child work. The
child's capabilities are an intersection::

    child = parent grant ∩ requested ceiling ∩ system policy

not a union, and budgets are carved from the parent, never additive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


class ChildStatus:
    SPAWNED = "spawned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"        # missing capability; returned, never retried silently
    INCOMPLETE = "incomplete"  # budget/timeout exhausted; typed partial report


class JoinPolicy:
    ALL = "all"        # wait for every child; one terminal failure invalidates
    ANY = "any"        # first valid result wins; cancel the rest
    QUORUM = "quorum"  # require N independently valid results


@dataclass
class ChildBudget:
    """Carved from the parent's remaining budget. Never additive."""
    model_calls: int = 6
    tool_calls: int = 12
    wall_seconds: int = 300

    def as_dict(self) -> dict:
        return {"model_calls": self.model_calls,
                "tool_calls": self.tool_calls,
                "wall_seconds": self.wall_seconds}


@dataclass
class DelegationRecord:
    """Durable delegation record (blueprint: delegation record)."""
    delegation_id: str
    parent_run_id: str
    child_run_id: str
    objective: str
    context_refs: list[dict] = field(default_factory=list)  # typed, bounded refs
    output_contract: dict = field(default_factory=dict)     # JSON schema
    capability_ceiling: list[str] = field(default_factory=list)
    effective_capabilities: list[str] = field(default_factory=list)  # after intersection
    explicit_writes: list[str] = field(default_factory=list)  # named external writes
    budget: ChildBudget = field(default_factory=ChildBudget)
    depth: int = 1
    join_policy: str = JoinPolicy.ALL
    status: str = ChildStatus.SPAWNED
    failure_code: str = ""
    failure_message: str = ""
    result: Optional["ChildResult"] = None
    result_versions: int = 0
    model_calls_used: int = 0
    tool_calls_used: int = 0
    created_at: float = 0.0
    closed: bool = False
    cancel_requested: bool = False

    def to_summary(self) -> dict:
        return {
            "delegation_id": self.delegation_id,
            "parent_run_id": self.parent_run_id,
            "child_run_id": self.child_run_id,
            "objective": self.objective,
            "status": self.status,
            "depth": self.depth,
            "budget": self.budget.as_dict(),
            "budget_used": {"model_calls": self.model_calls_used,
                            "tool_calls": self.tool_calls_used},
            "capability_ceiling": self.capability_ceiling,
            "failure_code": self.failure_code,
            "result_versions": self.result_versions,
            "closed": self.closed,
            "cancel_requested": self.cancel_requested,
        }


@dataclass
class ChildResult:
    """Typed handoff back to the parent turn.

    `output` should validate against the delegation's output_contract.
    Child results are DATA to the parent: they may contain mistakes or
    injected content, and the parent verifies consequential facts.
    """
    delegation_id: str
    status: str  # completed | contract_violation | blocked | incomplete | failed
    output: Any  # typed per output_contract when status == completed
    evidence_refs: list[str] = field(default_factory=list)
    raw_text: str = ""
    usage: dict = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "delegation_id": self.delegation_id,
            "status": self.status,
            "output": self.output,
            "evidence_refs": self.evidence_refs,
            "usage": self.usage,
            "notes": self.notes,
        }
