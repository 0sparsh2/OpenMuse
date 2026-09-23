"""
Durable run model.

A run is a persisted state machine; state transitions are validated here in
the domain layer, never inferred from which handler ran. Canonical states
mirror the blueprint's turn engine.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from gateway.protocol import ChatMessage

# Canonical states
RECEIVED = "RECEIVED"
ASSEMBLING_CONTEXT = "ASSEMBLING_CONTEXT"
AWAITING_MODEL = "AWAITING_MODEL"
EVALUATING_ACTIONS = "EVALUATING_ACTIONS"
WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
EXECUTING_TOOLS = "EXECUTING_TOOLS"
INGESTING_RESULTS = "INGESTING_RESULTS"
FINALIZING = "FINALIZING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
PAUSED = "PAUSED"            # user asked to pause; resumes at the same step boundary

TERMINAL = frozenset({COMPLETED, FAILED, CANCELLED})

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    RECEIVED: frozenset({ASSEMBLING_CONTEXT, CANCELLED, PAUSED}),
    ASSEMBLING_CONTEXT: frozenset({AWAITING_MODEL, FAILED, CANCELLED}),
    AWAITING_MODEL: frozenset({EVALUATING_ACTIONS, FINALIZING, FAILED, CANCELLED}),
    EVALUATING_ACTIONS: frozenset({WAITING_FOR_APPROVAL, EXECUTING_TOOLS, INGESTING_RESULTS, FAILED, CANCELLED}),
    WAITING_FOR_APPROVAL: frozenset({ASSEMBLING_CONTEXT, EVALUATING_ACTIONS, INGESTING_RESULTS, FAILED, CANCELLED}),
    PAUSED: frozenset({ASSEMBLING_CONTEXT, FAILED, CANCELLED}),
    EXECUTING_TOOLS: frozenset({INGESTING_RESULTS, FAILED, CANCELLED}),
    INGESTING_RESULTS: frozenset({ASSEMBLING_CONTEXT, FAILED, CANCELLED, PAUSED}),
    FINALIZING: frozenset({COMPLETED, FAILED, CANCELLED}),
    COMPLETED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
}


class InvalidTransitionError(ValueError):
    pass


@dataclass
class RunBudgets:
    max_model_calls: int = 18
    max_tool_calls: int = 40
    max_wall_seconds: int = 900


@dataclass
class Run:
    run_id: str
    tenant_id: str
    chat_id: str
    user_id: str
    state: str = RECEIVED
    messages: list[ChatMessage] = field(default_factory=list)
    loaded_namespaces: set[str] = field(default_factory=set)
    budgets: RunBudgets = field(default_factory=RunBudgets)
    model_calls_used: int = 0
    tool_calls_used: int = 0
    step: int = 0
    cancel_requested: bool = False
    pause_requested: bool = False
    failure_code: str = ""
    failure_message: str = ""
    final_text: str = ""
    idempotency_key: str = ""
    created_at: float = field(default_factory=time.time)

    def transition(self, new_state: str) -> None:
        if new_state not in _ALLOWED_TRANSITIONS[self.state]:
            raise InvalidTransitionError(f"{self.state} -> {new_state} is not a legal transition")
        self.state = new_state

    @property
    def wall_elapsed_s(self) -> float:
        return time.time() - self.created_at

    @property
    def remaining_budget_text(self) -> str:
        return (
            f"model_calls {self.budgets.max_model_calls - self.model_calls_used}/"
            f"{self.budgets.max_model_calls}, tool_calls "
            f"{self.budgets.max_tool_calls - self.tool_calls_used}/{self.budgets.max_tool_calls}, "
            f"wall {int(self.budgets.max_wall_seconds - self.wall_elapsed_s)}s left"
        )


class RunStore:
    """
    In-memory run registry with idempotent submission: a duplicate message
    submission (same idempotency key) returns the existing run instead of
    creating a second one. (Persistent backing store is a later-phase concern;
    the interface is already keyed for it.)
    """

    def __init__(self):
        self._runs: dict[str, Run] = {}
        self._by_idempotency: dict[str, str] = {}

    def submit(
        self,
        *,
        tenant_id: str,
        chat_id: str,
        user_id: str,
        user_message: ChatMessage,
        idempotency_key: str = "",
        budgets: Optional[RunBudgets] = None,
    ) -> tuple[Run, bool]:
        """Returns (run, created). created=False means this was a duplicate submission."""
        if idempotency_key and idempotency_key in self._by_idempotency:
            return self._runs[self._by_idempotency[idempotency_key]], False
        run = Run(
            run_id="run_" + uuid.uuid4().hex[:12],
            tenant_id=tenant_id,
            chat_id=chat_id,
            user_id=user_id,
            messages=[user_message],
            budgets=budgets or RunBudgets(),
            idempotency_key=idempotency_key,
        )
        self._runs[run.run_id] = run
        if idempotency_key:
            self._by_idempotency[idempotency_key] = run.run_id
        return run, True

    def get(self, run_id: str) -> Run:
        return self._runs[run_id]
