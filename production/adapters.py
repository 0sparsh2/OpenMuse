"""Backup/deletion participants for every store tier — Phase 8.

Each participant adapts one existing store to the BackupParticipant
protocol (snapshot / restore / delete_tenant / audit), so the disaster
recovery and tenant-deletion services cover the full blueprint surface:
database (run log), objects, vectors, vault, browser profiles, schedules,
queue, memory, and backups.
"""
from __future__ import annotations

import os
import shutil

from ._store import atomic_write_json, b64d, b64e, read_json

from .objects import ObjectStore
from .queue import RegionalQueue
from .runlog import RunLogStore


class DirParticipant:
    """Generic participant for a per-tenant directory tree.

    Snapshot captures every file (base64); restore replaces the tree;
    deletion removes it; the audit counts residuals.
    """

    def __init__(self, store_name: str, root: str):
        self.store_name = store_name
        self.root = root

    def _tenant_dir(self, tenant_id: str) -> str:
        return os.path.join(self.root, tenant_id)

    def _files(self, tenant_id: str) -> dict[str, str]:
        out: dict[str, str] = {}
        base = self._tenant_dir(tenant_id)
        if not os.path.isdir(base):
            return out
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, base)
                with open(full, "rb") as f:
                    out[rel] = b64e(f.read())
        return out

    def snapshot(self, tenant_id: str) -> dict:
        return {"files": self._files(tenant_id)}

    def restore(self, tenant_id: str, data: dict) -> None:
        base = self._tenant_dir(tenant_id)
        if os.path.isdir(base):
            shutil.rmtree(base)
        for rel, content_b64 in data.get("files", {}).items():
            full = os.path.join(base, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(b64d(content_b64))

    def delete_tenant(self, tenant_id: str) -> dict:
        base = self._tenant_dir(tenant_id)
        n = len(self._files(tenant_id))
        if os.path.isdir(base):
            shutil.rmtree(base)
        return {"files_removed": n}

    def audit(self, tenant_id: str) -> dict:
        return {"residual_files": len(self._files(tenant_id))}


class RunLogParticipant:
    """Database tier: the durable run-record log."""
    store_name = "database"

    def __init__(self, store: RunLogStore):
        self._store = store

    def snapshot(self, tenant_id: str) -> dict:
        return {"records": self._store.list(tenant_id)}

    def restore(self, tenant_id: str, data: dict) -> None:
        self._store.purge(tenant_id)
        for rec in data.get("records", []):
            body = {k: v for k, v in rec.items()
                    if k not in ("record_id", "tenant_id", "at")}
            self._store.append(tenant_id, body)

    def delete_tenant(self, tenant_id: str) -> dict:
        return {"records_removed": self._store.purge(tenant_id)}

    def audit(self, tenant_id: str) -> dict:
        return {"residual_records": self._store.count(tenant_id)}


class QueueParticipant:
    """Work-queue tier: the tenant's messages across all regions."""
    store_name = "queue"

    def __init__(self, queue: RegionalQueue):
        self._queue = queue

    def snapshot(self, tenant_id: str) -> dict:
        regions: dict[str, list[dict]] = {}
        for region in self._queue.regions:
            data = self._queue._load(region)
            regions[region] = [r for r in data.values()
                               if r.get("tenant_id") == tenant_id]
        return {"regions": regions}

    def restore(self, tenant_id: str, data: dict) -> None:
        self._queue.remove_tenant_messages(tenant_id)
        for region, records in data.get("regions", {}).items():
            if region not in self._queue.regions:
                continue
            store = self._queue._load(region)
            for rec in records:
                store[rec["message_id"]] = rec
            self._queue._save(region, store)

    def delete_tenant(self, tenant_id: str) -> dict:
        return {"messages_removed":
                self._queue.remove_tenant_messages(tenant_id)}

    def audit(self, tenant_id: str) -> dict:
        return {"residual_messages":
                len(self._queue.messages_for_tenant(tenant_id))}


class VaultParticipant:
    """Credential tier: the connector vault's tenant-bound refs."""
    store_name = "vault"

    def __init__(self, vault):
        self._vault = vault

    def snapshot(self, tenant_id: str) -> dict:
        creds = {}
        for ref in self._vault.refs_for_tenant(tenant_id):
            creds[ref] = dict(self._vault._secrets[ref])
        return {"credentials": creds}

    def restore(self, tenant_id: str, data: dict) -> None:
        for ref in self._vault.refs_for_tenant(tenant_id):
            self._vault._secrets.pop(ref, None)
        for ref, rec in data.get("credentials", {}).items():
            self._vault._secrets[ref] = dict(rec)

    def delete_tenant(self, tenant_id: str) -> dict:
        refs = self._vault.refs_for_tenant(tenant_id)
        for ref in refs:
            self._vault._secrets.pop(ref, None)
        return {"credentials_removed": len(refs)}

    def audit(self, tenant_id: str) -> dict:
        return {"residual_credentials":
                len(self._vault.refs_for_tenant(tenant_id))}


class SchedulerParticipant(DirParticipant):
    """Schedules tier: the scheduler's per-tenant durable state."""
    def __init__(self, root: str):
        super().__init__("schedules", root)


class BrowserProfileParticipant(DirParticipant):
    """Browser tier: persistent per-tenant browser profiles."""
    def __init__(self, root: str):
        super().__init__("browser_profiles", root)


class MemoryParticipant(DirParticipant):
    """Memory tier: the tenant's memory store directory."""
    def __init__(self, root: str):
        super().__init__("memory", root)


class ObjectStoreParticipant(DirParticipant):
    """Objects tier: the tenant's blob namespace."""
    def __init__(self, store: ObjectStore):
        super().__init__("objects", store.root)


class VectorIndexParticipant:
    """Vectors tier: the tenant's vector index file."""
    store_name = "vectors"

    def __init__(self, root: str):
        self.root = root

    def _path(self, tenant_id: str) -> str:
        return os.path.join(self.root, f"{tenant_id}.json")

    def snapshot(self, tenant_id: str) -> dict:
        return {"index": read_json(self._path(tenant_id), None)}

    def restore(self, tenant_id: str, data: dict) -> None:
        index = data.get("index")
        if index is None:
            if os.path.exists(self._path(tenant_id)):
                os.unlink(self._path(tenant_id))
        else:
            atomic_write_json(self._path(tenant_id), index)

    def delete_tenant(self, tenant_id: str) -> dict:
        existed = os.path.exists(self._path(tenant_id))
        if existed:
            os.unlink(self._path(tenant_id))
        return {"indexes_removed": 1 if existed else 0}

    def audit(self, tenant_id: str) -> dict:
        return {"residual_indexes":
                1 if os.path.exists(self._path(tenant_id)) else 0}


class BackupDirParticipant(DirParticipant):
    """Backups tier: the tenant's snapshot bundles (policy-gated purge)."""
    def __init__(self, root: str):
        super().__init__("backups", root)
