"""Core data models for the production layer — Phase 8."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from ._store import utcnow


def new_id(prefix: str) -> str:
    return f"{prefix}_" + uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Work queue
# ---------------------------------------------------------------------------
QUEUED = "queued"
CLAIMED = "claimed"
COMPLETED = "completed"
DEAD = "dead"


@dataclass
class WorkItem:
    """One unit of heavy work behind the queue (turn, browser run, job)."""
    message_id: str
    run_id: str
    tenant_id: str
    kind: str            # "turn" | "browser" | "scheduled" | "channel"
    payload: dict = field(default_factory=dict)
    idempotency_key: str = ""
    attempts: int = 0
    max_attempts: int = 3
    status: str = QUEUED
    region: str = ""
    created_at: str = ""
    claimed_by: str = ""
    claimed_at: str = ""
    completed_at: str = ""
    dead_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id, "run_id": self.run_id,
            "tenant_id": self.tenant_id, "kind": self.kind,
            "payload": self.payload, "idempotency_key": self.idempotency_key,
            "attempts": self.attempts, "max_attempts": self.max_attempts,
            "status": self.status, "region": self.region,
            "created_at": self.created_at, "claimed_by": self.claimed_by,
            "claimed_at": self.claimed_at, "completed_at": self.completed_at,
            "dead_reason": self.dead_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WorkItem":
        return cls(**{k: d.get(k, getattr(cls, k, "")) for k in (
            "message_id", "run_id", "tenant_id", "kind", "payload",
            "idempotency_key", "attempts", "max_attempts", "status", "region",
            "created_at", "claimed_by", "claimed_at", "completed_at",
            "dead_reason")})

    @classmethod
    def new(cls, *, run_id: str, tenant_id: str, kind: str,
            payload: dict | None = None, message_id: str = "",
            idempotency_key: str = "", max_attempts: int = 3) -> "WorkItem":
        return cls(
            message_id=message_id or new_id("msg"),
            run_id=run_id, tenant_id=tenant_id, kind=kind,
            payload=payload or {}, idempotency_key=idempotency_key,
            max_attempts=max_attempts, created_at=utcnow(),
        )


# ---------------------------------------------------------------------------
# Quotas / backpressure
# ---------------------------------------------------------------------------
@dataclass
class QuotaDecision:
    action: str      # "allow" | "queue" | "shed"
    reason: str
    tenant_id: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


# ---------------------------------------------------------------------------
# Approval deep links
# ---------------------------------------------------------------------------
@dataclass
class ApprovalLink:
    token: str
    url: str
    tenant_id: str
    approval_request_id: str
    run_id: str
    issued_at: str
    expires_at: str


# ---------------------------------------------------------------------------
# Disaster recovery
# ---------------------------------------------------------------------------
@dataclass
class BackupSnapshot:
    snapshot_id: str
    tenant_id: str
    created_at: str
    stores: dict          # store_name -> {"records": n, "bytes": n}
    sha256: str
    path: str


@dataclass
class RestoreReport:
    snapshot_id: str
    tenant_id: str
    rto_seconds: float
    rpo_seconds: float
    rto_target_s: float
    rpo_target_s: float
    stores_restored: list
    passed: bool
    stores_skipped: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id, "tenant_id": self.tenant_id,
            "rto_seconds": round(self.rto_seconds, 3),
            "rpo_seconds": round(self.rpo_seconds, 3),
            "rto_target_s": self.rto_target_s, "rpo_target_s": self.rpo_target_s,
            "stores_restored": self.stores_restored,
            "stores_skipped": self.stores_skipped,
            "rto_ok": self.rto_seconds <= self.rto_target_s,
            "rpo_ok": self.rpo_seconds <= self.rpo_target_s,
            "passed": self.passed,
        }


@dataclass
class DeletionAudit:
    tenant_id: str
    removed: dict          # store_name -> {"removed": n, ...}
    residuals: dict        # store_name -> {"residual_xyz": n, ...}
    passed: bool

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id, "removed": self.removed,
            "residuals": self.residuals, "passed": self.passed,
        }


# ---------------------------------------------------------------------------
# Load testing
# ---------------------------------------------------------------------------
@dataclass
class LoadReport:
    name: str
    concurrency: int
    duration_s: float
    calls: int
    throughput_rps: float
    p50_ms: float
    p99_ms: float
    max_ms: float
    errors: int
    target_p99_ms: float
    target_min_rps: float
    passed: bool

    def to_dict(self) -> dict:
        return {
            "name": self.name, "concurrency": self.concurrency,
            "duration_s": round(self.duration_s, 2), "calls": self.calls,
            "throughput_rps": round(self.throughput_rps, 1),
            "p50_ms": round(self.p50_ms, 2), "p99_ms": round(self.p99_ms, 2),
            "max_ms": round(self.max_ms, 2), "errors": self.errors,
            "target_p99_ms": self.target_p99_ms,
            "target_min_rps": self.target_min_rps, "passed": self.passed,
        }
