"""
Seams for later phases — interfaces only, no implementations.

Phase 3 (subagents), Phase 4 (browser computer use), and Phase 5 (scheduler)
plug in here. Each seam raises NotImplementedError with the phase that owns
it, so accidental use fails loudly instead of silently misbehaving.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Phase 3 — subagents and delegation
# ---------------------------------------------------------------------------
@dataclass
class DelegationRequest:
    """A focused unit of work for a child run (blueprint: delegation record)."""
    task: str
    allowed_namespaces: list[str]          # capability ceiling for the child
    budget_carve: dict = field(default_factory=dict)  # carved from parent, never additive
    max_depth: int = 1
    # Phase 3 extensions (all optional; older callers keep working).
    parent_run_id: str = ""
    output_contract: dict = field(default_factory=dict)  # JSON schema for the handoff
    context_refs: list = field(default_factory=list)     # typed, bounded refs only
    join_policy: str = "all"
    explicit_writes: list[str] = field(default_factory=list)  # named external writes


class SubagentRunner(abc.ABC):
    @abc.abstractmethod
    def spawn(self, request: DelegationRequest) -> str:
        """Start a child run; return its run id. Phase 3."""
        raise NotImplementedError("subagents arrive in Phase 3")


# ---------------------------------------------------------------------------
# Phase 4 — browser computer use
# ---------------------------------------------------------------------------
@dataclass
class BrowserObservation:
    url: str
    title: str
    accessibility_tree: str = ""
    screenshot_ref: str = ""
    # Phase 4 extensions (all optional; older readers keep working).
    navigation_id: int = 0
    origin: str = ""
    interactive_elements: list = field(default_factory=list)  # stable el_<nav>_<n> ids
    forms: list = field(default_factory=list)
    challenges: list = field(default_factory=list)  # e.g. ["captcha"]
    session_state: str = "active"  # active | challenged | closed
    captured_at: str = ""


class BrowserOperator(abc.ABC):
    @abc.abstractmethod
    def observe(self, session_id: str) -> BrowserObservation:
        raise NotImplementedError("browser computer use arrives in Phase 4")

    @abc.abstractmethod
    def act(self, session_id: str, action: dict) -> dict:
        """Execute one grounded action; return a result envelope dict.

        The envelope always carries the fresh observation; it may also carry
        a challenge handoff, a commit proposal, or a deterministic refusal
        (stale element, pacing, egress, evasion). Phase 4.
        """
        raise NotImplementedError("browser computer use arrives in Phase 4")

    # -- Phase 4 session lifecycle (concrete defaults; the operator overrides)
    def start_session(self, tenant_id: str) -> str:
        raise NotImplementedError("browser computer use arrives in Phase 4")

    def close_session(self, session_id: str) -> dict:
        raise NotImplementedError("browser computer use arrives in Phase 4")

    def checkpoint(self, session_id: str, label: str) -> dict:
        raise NotImplementedError("browser computer use arrives in Phase 4")

    def recover(self, session_id: str) -> dict:
        raise NotImplementedError("browser computer use arrives in Phase 4")

    def attach_session(self, session_id: str) -> dict:
        """Re-attach a persisted session (e.g. after operator restart). Phase 4."""
        raise NotImplementedError("browser computer use arrives in Phase 4")

    def mark_challenge_resolved(self, session_id: str, *, by: str = "user") -> dict:
        """Record that the USER resolved a challenge out-of-band. Phase 4."""
        raise NotImplementedError("browser computer use arrives in Phase 4")


# ---------------------------------------------------------------------------
# Phase 5 — scheduling and event-driven hooks
# ---------------------------------------------------------------------------
class Scheduler(abc.ABC):
    @abc.abstractmethod
    def add_cron(self, *, name: str, schedule: str, timezone: str, instructions: str) -> str:
        """Register a cron job; return its id. Phase 5."""
        raise NotImplementedError("scheduling arrives in Phase 5")

    @abc.abstractmethod
    def register_hook(self, *, name: str, event: str, instructions: str) -> str:
        """Register an event-driven hook; return its id. Phase 5."""
        raise NotImplementedError("hooks arrive in Phase 5")


# ---------------------------------------------------------------------------
# Phase 6 — connectors and secure credential use
# ---------------------------------------------------------------------------
class ConnectorProvider(abc.ABC):
    """Seam for authenticated third-party service access.

    The concrete implementation lives in connectors/ (ConnectorRegistry).
    Tools and runs see connection ids only; the registry resolves vault
    references into short-lived handles scoped to one call.
    """

    @abc.abstractmethod
    def status(self, *, tenant_id: str, connection_id: str) -> dict:
        """Connection health + declared capabilities; never credential material."""
        raise NotImplementedError("connectors arrive in Phase 6")

    @abc.abstractmethod
    def call(self, *, run_id: str, tenant_id: str, connection_id: str,
             op: str, args: dict) -> dict:
        """Execute one declared operation under scope and policy enforcement."""
        raise NotImplementedError("connectors arrive in Phase 6")
