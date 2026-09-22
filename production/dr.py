"""Disaster recovery and tenant deletion — Phase 8.

BackupService snapshots every registered store for a tenant into one
checksummed bundle (0600); restore replays a snapshot and measures the
recovery against stated RTO/RPO objectives. TenantDeletionService removes
a tenant's data across every store — database, objects, vectors, vault,
browser profiles, schedules, queue, memory, backups — then audits every
store for residuals and proves zero remain.

A BackupParticipant is any store wrapper implementing:
  store_name: str
  snapshot(tenant_id) -> dict        (JSON-serializable)
  restore(tenant_id, data: dict)     (replace tenant state with snapshot)
  delete_tenant(tenant_id) -> dict   ({"removed": n, ...})
  audit(tenant_id) -> dict           ({"residual_<thing>": n, ...})

Mirrors the blueprint's Phase 8 "Disaster recovery, export/delete flows,
and compliance controls" and the exit criteria "Restore exercises meet
recovery objectives" / "Tenant deletion removes data across database,
objects, vectors, vault, browser profiles, and backups according to
policy".
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Protocol

from ._store import atomic_write_json, read_json, utcnow
from .models import BackupSnapshot, DeletionAudit, RestoreReport, new_id


class BackupParticipant(Protocol):
    store_name: str

    def snapshot(self, tenant_id: str) -> dict: ...
    def restore(self, tenant_id: str, data: dict) -> None: ...
    def delete_tenant(self, tenant_id: str) -> dict: ...
    def audit(self, tenant_id: str) -> dict: ...


class BackupService:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _snap_path(self, tenant_id: str, snapshot_id: str) -> str:
        return os.path.join(self.root, tenant_id, f"{snapshot_id}.json")

    def snapshot_tenant(self, tenant_id: str,
                        participants: list[BackupParticipant]) -> BackupSnapshot:
        snapshot_id = new_id("snap")
        stores: dict[str, dict] = {}
        payload: dict[str, dict] = {}
        for p in participants:
            data = p.snapshot(tenant_id)
            payload[p.store_name] = data
            raw = json.dumps(data, sort_keys=True, ensure_ascii=False,
                             default=str)
            stores[p.store_name] = {"bytes": len(raw.encode())}
        envelope = {"snapshot_id": snapshot_id, "tenant_id": tenant_id,
                    "created_at": utcnow(), "stores": stores,
                    "payload": payload}
        canonical = json.dumps(envelope, sort_keys=True, ensure_ascii=False)
        envelope["sha256"] = ("sha256:" +
                              hashlib.sha256(canonical.encode()).hexdigest())
        path = self._snap_path(tenant_id, snapshot_id)
        atomic_write_json(path, envelope)
        os.chmod(path, 0o600)
        return BackupSnapshot(
            snapshot_id=snapshot_id, tenant_id=tenant_id,
            created_at=envelope["created_at"], stores=stores,
            sha256=envelope["sha256"], path=path)

    def list_snapshots(self, tenant_id: str) -> list[str]:
        d = os.path.join(self.root, tenant_id)
        if not os.path.isdir(d):
            return []
        return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))

    def restore_tenant(self, tenant_id: str, snapshot_id: str,
                       participants: list[BackupParticipant], *,
                       rto_target_s: float = 300.0,
                       rpo_target_s: float = 3600.0,
                       exclude: tuple[str, ...] = ("backups",)
                       ) -> RestoreReport:
        """Restore and measure against objectives.

        RTO = wall time to complete the restore. RPO = data-loss window =
        time between the snapshot and the restore (the freshest state the
        snapshot could not contain).

        The backups tier is excluded by default: the snapshot catalog is
        governed by retention policy, and replaying it would delete the
        snapshot being restored from.
        """
        path = self._snap_path(tenant_id, snapshot_id)
        envelope = read_json(path, None)
        if envelope is None:
            raise KeyError(f"unknown snapshot {snapshot_id}")
        by_name = {p.store_name: p for p in participants}
        restored: list[str] = []
        skipped: list[str] = []
        start = time.time()
        for store_name, data in envelope["payload"].items():
            if store_name in exclude:
                skipped.append(store_name)
                continue
            participant = by_name.get(store_name)
            if participant is None:
                continue
            participant.restore(tenant_id, data)
            restored.append(store_name)
        rto = time.time() - start
        snap_ts = envelope["created_at"]
        try:
            from datetime import datetime
            snap_dt = datetime.fromisoformat(snap_ts)
            rpo = max(0.0, (datetime.now(snap_dt.tzinfo) - snap_dt
                             ).total_seconds())
        except Exception:
            rpo = 0.0
        passed = rto <= rto_target_s and rpo <= rpo_target_s
        return RestoreReport(
            snapshot_id=snapshot_id, tenant_id=tenant_id,
            rto_seconds=rto, rpo_seconds=rpo,
            rto_target_s=rto_target_s, rpo_target_s=rpo_target_s,
            stores_restored=restored, passed=passed,
            stores_skipped=skipped)


class TenantDeletionService:
    """Delete a tenant everywhere, then prove it with a residual audit."""

    def delete_tenant(self, tenant_id: str,
                      participants: list[BackupParticipant], *,
                      purge_backups: bool = True,
                      backup_store_names: tuple[str, ...] = ("backups",)
                      ) -> DeletionAudit:
        removed: dict[str, dict] = {}
        for p in participants:
            if not purge_backups and p.store_name in backup_store_names:
                removed[p.store_name] = {"removed": 0, "skipped": "policy"}
                continue
            removed[p.store_name] = p.delete_tenant(tenant_id)
        residuals: dict[str, dict] = {}
        for p in participants:
            if not purge_backups and p.store_name in backup_store_names:
                residuals[p.store_name] = {"residual": "skipped_by_policy"}
                continue
            residuals[p.store_name] = p.audit(tenant_id)
        passed = all(
            all(v == 0 for v in r.values() if isinstance(v, int))
            for r in residuals.values()
        )
        return DeletionAudit(tenant_id=tenant_id, removed=removed,
                             residuals=residuals, passed=passed)
