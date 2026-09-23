"""
Approval service — user-visible, cryptographically bound grants.

A grant binds: user/tenant, run, tool name+version, canonical argument hash,
and selected human-readable fields. If any bound field changes, the grant is
invalid — "yes" to one call never authorizes a later or different call.

Phase 1 ships two deciders:
  - ManualDecider: leaves requests PENDING (the turn engine parks in
    WAITING_FOR_APPROVAL; a client resolves them later — seam for the web UI).
  - AutoApproveDecider: DEMO/TEST ONLY. Approves R2 local-write requests whose
    arguments stay inside the workspace. Never use in production.
"""
from __future__ import annotations

import abc
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class ApprovalRequest:
    id: str
    run_id: str
    tenant_id: str
    tool_name: str
    tool_version: str
    argument_hash: str
    bind_fields: dict          # human-readable, secret-free
    risk: str
    template: str
    status: str = "pending"    # pending | approved | denied | expired
    created_at: float = field(default_factory=time.time)
    expires_in_seconds: int = 900
    decided_by: str = ""


@dataclass
class ApprovalGrant:
    id: str
    request_id: str
    run_id: str
    tool_name: str
    tool_version: str
    argument_hash: str         # the EXACT canonical args this grant covers
    single_use: bool = True
    used: bool = False
    expires_at: float = 0.0


class ApprovalDecider(abc.ABC):
    @abc.abstractmethod
    def decide(self, request: ApprovalRequest) -> str:
        """Return 'approved' or 'denied'. May also leave pending by raising PendingApproval."""
        raise NotImplementedError


class PendingApproval(Exception):
    """Raised by ManualDecider: the run must park in WAITING_FOR_APPROVAL."""


class ManualDecider(ApprovalDecider):
    """Production path: a human resolves the request out of band."""

    def decide(self, request: ApprovalRequest) -> str:
        raise PendingApproval(request.id)


class AutoApproveDecider(ApprovalDecider):
    """DEMO/TEST ONLY. Auto-approves R1/R2 requests. Never ship this."""

    def __init__(self, *, allow_risks=("R1", "R2")):
        self.allow_risks = set(allow_risks)

    def decide(self, request: ApprovalRequest) -> str:
        if request.risk in self.allow_risks:
            return "approved"
        return "denied"


class ApprovalService:
    def __init__(self):
        self.requests: dict[str, ApprovalRequest] = {}
        self.grants: dict[str, ApprovalGrant] = {}

    def create_request(
        self, *, run_id: str, tenant_id: str, tool_name: str, tool_version: str,
        argument_hash: str, bind_fields: dict, risk: str, template: str,
    ) -> ApprovalRequest:
        req = ApprovalRequest(
            id="apr_" + uuid.uuid4().hex[:12],
            run_id=run_id, tenant_id=tenant_id,
            tool_name=tool_name, tool_version=tool_version,
            argument_hash=argument_hash, bind_fields=bind_fields,
            risk=risk, template=template,
        )
        self.requests[req.id] = req
        return req

    def resolve(self, request_id: str, decision: str, *, decided_by: str = "") -> ApprovalGrant | None:
        req = self.requests[request_id]
        if req.status != "pending":
            raise ValueError(f"approval {request_id} already {req.status}")
        if decision not in ("approved", "denied"):
            raise ValueError("decision must be 'approved' or 'denied'")
        req.status = decision
        req.decided_by = decided_by
        if decision == "denied":
            return None
        grant = ApprovalGrant(
            id="grt_" + uuid.uuid4().hex[:12],
            request_id=req.id, run_id=req.run_id,
            tool_name=req.tool_name, tool_version=req.tool_version,
            argument_hash=req.argument_hash,
            expires_at=time.time() + req.expires_in_seconds,
        )
        self.grants[grant.id] = grant
        return grant

    def find_valid_grant(self, *, run_id: str, tool_name: str, tool_version: str, argument_hash: str) -> ApprovalGrant | None:
        """A grant is valid only for the exact bound argument hash, unexpired and unused."""
        now = time.time()
        for grant in self.grants.values():
            if (
                grant.run_id == run_id
                and grant.tool_name == tool_name
                and grant.tool_version == tool_version
                and grant.argument_hash == argument_hash
                and not grant.used
                and grant.expires_at > now
            ):
                return grant
        return None

    def consume(self, grant: ApprovalGrant) -> None:
        if grant.single_use:
            grant.used = True


class AutonomousDecider(ApprovalDecider):
    """User-enabled autonomy: reversible local steps run without a prompt.

    Auto-approves R1/R2 requests whose effect stays local and reversible —
    browsing (navigate/click/type/select/scroll/wait/back), reading memory,
    notes, workspace files. Everything else falls through to the human
    (ManualDecider -> WAITING_FOR_APPROVAL): browser commits, uploads and
    credential fills, shell, account connections, memory deletion,
    schedules, production/admin tools, any external write, and R3+.
    The operator's commit barrier still stops purchase-like clicks, and every
    auto-approval is still a recorded, argument-bound, single-use grant.
    """

    AUTO_RISKS = {"R1", "R2"}
    HOLD_PREFIXES = ("shell.", "connector.", "production.", "scheduler.")
    HOLD_TOOLS = {"memory.forget", "subagent.spawn", "subagent.send"}
    SAFE_BROWSER_KINDS = {"navigate", "click", "type", "select", "scroll", "wait", "back"}

    def __init__(self, fallback: ApprovalDecider | None = None):
        self.fallback = fallback or ManualDecider()

    def decide(self, request: ApprovalRequest) -> str:
        tool = request.tool_name
        held = (request.risk not in self.AUTO_RISKS
                or tool in self.HOLD_TOOLS
                or tool.startswith(self.HOLD_PREFIXES))
        if tool == "browser.act":
            action = (request.bind_fields or {}).get("action") or {}
            held = held or action.get("kind") not in self.SAFE_BROWSER_KINDS \
                or bool(action.get("text_ref"))
        if held:
            return self.fallback.decide(request)
        return "approved"
