"""Phase 3 — skills and subagents.

A subagent is a child run with a focused objective, bounded context, bounded
budget, and an explicit return contract. It is not a new source of authority.

Modules:
  models      — delegation records, budgets, results, join policies.
  skills      — versioned skill catalog, loader, validator, selection.
  runner      — child-run lifecycle: spawn / status / send / close / cancel.
  coordinator — fan-out/fan-in, pipeline joins, typed parent synthesis.
  namespace   — the `subagents` tool namespace (subagent.spawn, ...).
"""
from __future__ import annotations

from subagents.models import (
    ChildBudget,
    ChildResult,
    ChildStatus,
    DelegationRecord,
    JoinPolicy,
)

__all__ = [
    "ChildBudget",
    "ChildResult",
    "ChildStatus",
    "DelegationRecord",
    "JoinPolicy",
]
