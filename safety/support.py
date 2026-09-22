"""Support access controls + privacy-preserving diagnostics (Phase 10).

Support staff never get open-ended data access:

- SupportAccessService issues scoped, time-boxed, single-purpose grants.
  Allowed scopes are metadata/diagnostics/policy only — there is deliberately
  no scope that exposes raw user content. Every grant and every access is
  appended to an audit trail. Expired or over-scoped access fails closed.
- DiagnosticsBuilder turns an event log into a diagnostic bundle that
  contains counts, hashes, error codes, and policy reason codes — never raw
  user data (message text, tool arguments, memory content are stripped or
  hashed). It additionally runs the secret scanner over the bundle before
  release.
- quarantine_run freezes a run after a policy-violation incident: pending
  approvals for the run are revoked (marked denied) and an incident record
  is returned for the runbook flow.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field

from safety.secret_scan import scan_persist

# The only scopes support grants may carry. No raw-content scope exists.
ALLOWED_SCOPES = frozenset({
    "diagnostics.read",   # redacted diagnostic bundles
    "policy.read",        # risk mappings, catalog, decisions (reason codes only)
    "runs.read-metadata", # run state machine states, counts, timestamps
})

MAX_GRANT_SECONDS = 3600  # support grants last at most one hour


class SupportAccessError(ValueError):
    pass


@dataclass
class SupportGrant:
    id: str
    tenant_id: str
    scopes: tuple[str, ...]
    purpose: str
    created_by: str
    created_at: float = field(default_factory=time.time)
    expires_in_seconds: int = 900
    revoked: bool = False

    @property
    def expired(self) -> bool:
        return time.time() > self.created_at + self.expires_in_seconds


@dataclass
class SupportAccessEvent:
    grant_id: str
    scope: str
    allowed: bool
    at: float = field(default_factory=time.time)
    note: str = ""


class SupportAccessService:
    def __init__(self):
        self.grants: dict[str, SupportGrant] = {}
        self.audit: list[SupportAccessEvent] = []

    def grant(
        self, *, tenant_id: str, scopes: list[str], purpose: str,
        created_by: str, expires_in_seconds: int = 900,
    ) -> SupportGrant:
        bad = [s for s in scopes if s not in ALLOWED_SCOPES]
        if bad:
            raise SupportAccessError(f"scopes not grantable to support: {bad}")
        if expires_in_seconds > MAX_GRANT_SECONDS:
            raise SupportAccessError(
                f"support grants are capped at {MAX_GRANT_SECONDS}s")
        if not purpose.strip():
            raise SupportAccessError("a purpose is required for support access")
        g = SupportGrant(
            id="sup_" + uuid.uuid4().hex[:12], tenant_id=tenant_id,
            scopes=tuple(scopes), purpose=purpose, created_by=created_by,
            expires_in_seconds=expires_in_seconds,
        )
        self.grants[g.id] = g
        self.audit.append(SupportAccessEvent(g.id, ",".join(scopes), True, note="grant issued"))
        return g

    def check(self, grant_id: str, scope: str, *, note: str = "") -> bool:
        g = self.grants.get(grant_id)
        allowed = bool(g and not g.revoked and not g.expired and scope in g.scopes)
        self.audit.append(SupportAccessEvent(grant_id, scope, allowed, note=note))
        return allowed

    def revoke(self, grant_id: str) -> None:
        g = self.grants.get(grant_id)
        if g:
            g.revoked = True
            self.audit.append(SupportAccessEvent(grant_id, "*", False, note="grant revoked"))


class DiagnosticsBuilder:
    """Builds privacy-preserving diagnostic bundles from an event log.

    The bundle contains only: event types, sequence/timing metadata,
    policy reason codes, error codes, and SHA-256 hashes of redacted
    payloads. Raw user data never appears — message text, tool arguments,
    and memory content are stripped before hashing, and the final bundle
    is secret-scanned before release.
    """

    def __init__(self, support: SupportAccessService):
        self.support = support

    def build(self, *, grant_id: str, event_log, tenant_id: str) -> dict:
        if not self.support.check(grant_id, "diagnostics.read", note="diagnostics.build"):
            raise SupportAccessError("support grant missing/expired for diagnostics.read")
        g = self.support.grants[grant_id]
        if g.tenant_id != tenant_id:
            raise SupportAccessError("support grant is scoped to a different tenant")

        by_type: dict[str, int] = {}
        reason_codes: dict[str, int] = {}
        first_ts, last_ts = None, None
        for evt in event_log.events:
            by_type[evt.type] = by_type.get(evt.type, 0) + 1
            rc = (evt.payload or {}).get("reason") or (evt.payload or {}).get("reason_code")
            if rc:
                reason_codes[rc] = reason_codes.get(rc, 0) + 1
            first_ts = first_ts or evt.occurred_at
            last_ts = evt.occurred_at

        bundle = {
            "tenant_id": tenant_id,
            "grant_id": grant_id,
            "event_count": len(event_log.events),
            "events_by_type": by_type,
            "policy_reason_codes": reason_codes,
            "window": {"first": first_ts, "last": last_ts},
            "log_integrity": _hash_chain(event_log),
        }
        # Final gate: the bundle itself must not leak secrets.
        scan = scan_persist(str(bundle))
        if scan.blocked:
            raise SupportAccessError(
                f"diagnostic bundle failed secret scan: {scan.labels}")
        bundle["secret_scan"] = "clean"
        return bundle


def _hash_chain(event_log) -> str:
    h = hashlib.sha256()
    for evt in event_log.events:
        h.update(f"{evt.sequence}:{evt.type}:{evt.payload_sha256}".encode())
    return "sha256:" + h.hexdigest()


def quarantine_run(*, run_id: str, approvals, reason: str) -> dict:
    """Freeze a run after a policy-violation incident.

    Revokes (denies) every pending approval for the run so nothing parked
    can be released afterwards, and returns the incident record the
    runbook flow (safety/runbooks.md RB-1) consumes.
    """
    revoked = []
    for req in approvals.requests.values():
        if req.run_id == run_id and req.status == "pending":
            req.status = "denied"
            req.decided_by = "incident-quarantine"
            revoked.append(req.id)
    return {
        "incident_id": "inc_" + uuid.uuid4().hex[:12],
        "run_id": run_id,
        "reason": reason,
        "quarantined_at": time.time(),
        "revoked_approvals": revoked,
        "next": "follow safety/runbooks.md RB-1 (preserve event log, notify, review)",
    }
