"""Durable regional work queues — Phase 8.

Heavy work (turn execution, browser runs, scheduled jobs) is submitted here
by the API process and claimed by the worker pool. Each region has its own
durable queue (atomic JSON files); queue replay is idempotent — submitting
a message id that was already seen returns the original record instead of
duplicating the work.

Regional failure: `fail_region` marks a region unhealthy (workers stop
claiming there); `drain_region` moves queued and claimed-but-unacked
messages to healthy regions with attempts preserved. Completed and dead
messages never move, so a regional failure cannot corrupt run state or
double-execute completed work.

Mirrors the blueprint's Phase 8 "regional queues" and the exit criteria
"Regional failure does not corrupt run state" / "Queue replay remains
idempotent". The interface is keyed for a real queue backend (SQS/Kafka);
this file-backed implementation is the deterministic offline substitute.
"""
from __future__ import annotations

import os
import threading

from ._store import atomic_write_json, read_json, utcnow
from .models import CLAIMED, COMPLETED, DEAD, QUEUED, WorkItem


class RegionDown(Exception):
    pass


class RegionalQueue:
    """Durable per-region queues with idempotent submit and failover drain."""

    def __init__(self, root: str, regions: list[str]):
        self.root = root
        self.regions = list(regions)
        # Serializes read-modify-write cycles: the file-backed store is the
        # offline substitute for a real queue backend's atomic operations.
        self._lock = threading.RLock()
        for region in self.regions:
            os.makedirs(self._region_dir(region), exist_ok=True)
        # region health persists so a restart does not resurrect a dead region
        self._health_path = os.path.join(root, "region_health.json")
        health = read_json(self._health_path, {})
        for region in self.regions:
            health.setdefault(region, True)
        atomic_write_json(self._health_path, health)

    # -- paths ------------------------------------------------------------
    def _region_dir(self, region: str) -> str:
        return os.path.join(self.root, "regions", region)

    def _messages_path(self, region: str) -> str:
        return os.path.join(self._region_dir(region), "messages.json")

    # -- health -----------------------------------------------------------
    def is_healthy(self, region: str) -> bool:
        return bool(read_json(self._health_path, {}).get(region, True))

    def fail_region(self, region: str) -> None:
        health = read_json(self._health_path, {})
        health[region] = False
        atomic_write_json(self._health_path, health)

    def heal_region(self, region: str) -> None:
        health = read_json(self._health_path, {})
        health[region] = True
        atomic_write_json(self._health_path, health)

    def healthy_regions(self) -> list[str]:
        return [r for r in self.regions if self.is_healthy(r)]

    # -- message storage --------------------------------------------------
    def _load(self, region: str) -> dict:
        return read_json(self._messages_path(region), {})

    def _save(self, region: str, data: dict) -> None:
        atomic_write_json(self._messages_path(region), data)

    def _seen_ids(self) -> set[str]:
        seen: set[str] = set()
        for region in self.regions:
            seen.update(self._load(region).keys())
        return seen

    # -- submit (idempotent) ----------------------------------------------
    def submit(self, item: WorkItem, *, region: str = "") -> tuple[str, bool]:
        """Submit a work item. Returns (message_id, is_duplicate).

        A replayed message id (or idempotency key) never creates a second
        record: the original is returned with is_duplicate=True.
        """
        with self._lock:
            return self._submit_locked(item, region=region)

    def _submit_locked(self, item: WorkItem, *, region: str = "") -> tuple[str, bool]:
        target = region or self._preferred_region()
        if item.message_id in self._seen_ids():
            return item.message_id, True
        if item.idempotency_key:
            for r in self.regions:
                for existing in self._load(r).values():
                    if existing.get("idempotency_key") == item.idempotency_key:
                        return existing["message_id"], True
        item.region = target
        item.status = QUEUED
        data = self._load(target)
        data[item.message_id] = item.to_dict()
        self._save(target, data)
        return item.message_id, False

    def _preferred_region(self) -> str:
        healthy = self.healthy_regions()
        if not healthy:
            raise RegionDown("no healthy regions available")
        # least-loaded healthy region
        return min(healthy, key=lambda r: self.depth(r))

    # -- claim / ack / nack ------------------------------------------------
    def claim(self, region: str, worker_id: str) -> WorkItem | None:
        """Claim the oldest queued message in a healthy region."""
        with self._lock:
            return self._claim_locked(region, worker_id)

    def _claim_locked(self, region: str, worker_id: str) -> WorkItem | None:
        if not self.is_healthy(region):
            raise RegionDown(f"region {region!r} is down")
        data = self._load(region)
        for mid in sorted(data, key=lambda m: data[m].get("created_at", "")):
            rec = data[mid]
            if rec["status"] == QUEUED:
                rec["status"] = CLAIMED
                rec["claimed_by"] = worker_id
                rec["claimed_at"] = utcnow()
                rec["attempts"] = rec.get("attempts", 0) + 1
                self._save(region, data)
                return WorkItem.from_dict(rec)
        return None

    def ack(self, region: str, message_id: str) -> None:
        with self._lock:
            data = self._load(region)
            rec = data.get(message_id)
            if rec is None:
                raise KeyError(f"unknown message {message_id}")
            rec["status"] = COMPLETED
            rec["completed_at"] = utcnow()
            self._save(region, data)

    def nack(self, region: str, message_id: str) -> str:
        """Negative-ack: requeue unless attempts are exhausted (-> dead)."""
        with self._lock:
            data = self._load(region)
            rec = data.get(message_id)
            if rec is None:
                raise KeyError(f"unknown message {message_id}")
            if rec.get("attempts", 0) >= rec.get("max_attempts", 3):
                rec["status"] = DEAD
                rec["dead_reason"] = "max_attempts_exceeded"
                self._save(region, data)
                return DEAD
            rec["status"] = QUEUED
            rec["claimed_by"] = ""
            rec["claimed_at"] = ""
            self._save(region, data)
            return QUEUED

    def defer(self, region: str, message_id: str) -> None:
        """Backpressure: return to queued without consuming an attempt."""
        with self._lock:
            data = self._load(region)
            rec = data.get(message_id)
            if rec is None:
                raise KeyError(f"unknown message {message_id}")
            rec["status"] = QUEUED
            rec["claimed_by"] = ""
            rec["claimed_at"] = ""
            self._save(region, data)

    def mark_dead(self, region: str, message_id: str, reason: str) -> None:
        with self._lock:
            data = self._load(region)
            rec = data.get(message_id)
            if rec is None:
                raise KeyError(f"unknown message {message_id}")
            rec["status"] = DEAD
            rec["dead_reason"] = reason
            self._save(region, data)

    # -- failover -----------------------------------------------------------
    def drain_region(self, source: str) -> dict:
        """Move queued + claimed-but-unacked messages to healthy regions.

        Completed and dead messages never move. Attempts are preserved so a
        retried message does not get a fresh attempt budget after failover.
        Returns {"moved": n, "targets": {region: n}}.
        """
        with self._lock:
            return self._drain_locked(source)

    def _drain_locked(self, source: str) -> dict:
        targets = self.healthy_regions()
        if source in targets:
            raise ValueError(f"region {source!r} is healthy; nothing to drain")
        if not targets:
            raise RegionDown("no healthy region to drain into")
        data = self._load(source)
        moved = 0
        per_target: dict[str, int] = {}
        remaining: dict[str, dict] = {}
        for mid, rec in data.items():
            if rec["status"] in (QUEUED, CLAIMED):
                dest = min(targets, key=lambda r: self.depth(r))
                rec["status"] = QUEUED
                rec["region"] = dest
                rec["claimed_by"] = ""
                rec["claimed_at"] = ""
                dest_data = self._load(dest)
                dest_data[mid] = rec
                self._save(dest, dest_data)
                moved += 1
                per_target[dest] = per_target.get(dest, 0) + 1
            else:
                remaining[mid] = rec
        self._save(source, remaining)
        return {"moved": moved, "targets": per_target}

    # -- inspection ----------------------------------------------------------
    def get(self, message_id: str) -> WorkItem | None:
        for region in self.regions:
            rec = self._load(region).get(message_id)
            if rec is not None:
                return WorkItem.from_dict(rec)
        return None

    def depth(self, region: str) -> int:
        return sum(1 for r in self._load(region).values()
                   if r["status"] == QUEUED)

    def messages_for_tenant(self, tenant_id: str) -> list[WorkItem]:
        out = []
        for region in self.regions:
            for rec in self._load(region).values():
                if rec.get("tenant_id") == tenant_id:
                    out.append(WorkItem.from_dict(rec))
        return out

    def remove_tenant_messages(self, tenant_id: str) -> int:
        removed = 0
        for region in self.regions:
            data = self._load(region)
            doomed = [m for m, r in data.items()
                      if r.get("tenant_id") == tenant_id]
            for m in doomed:
                del data[m]
                removed += 1
            if doomed:
                self._save(region, data)
        return removed
