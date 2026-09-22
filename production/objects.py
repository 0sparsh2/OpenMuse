"""Durable object store (the "objects" tier) — Phase 8.

Tenant-scoped, file-backed blob storage with atomic writes: the offline
substitute for the object tier (S3/GCS) that artifacts and exports live in.
"""
from __future__ import annotations

import os

from ._store import atomic_write_json, b64d, b64e, read_json, sanitize_key, utcnow


class ObjectStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _tenant_dir(self, tenant_id: str) -> str:
        d = os.path.join(self.root, tenant_id)
        os.makedirs(d, exist_ok=True)
        return d

    def _meta_path(self, tenant_id: str) -> str:
        return os.path.join(self._tenant_dir(tenant_id), "_meta.json")

    def put(self, tenant_id: str, key: str, data: bytes,
            content_type: str = "application/octet-stream") -> dict:
        safe = sanitize_key(key)
        with open(os.path.join(self._tenant_dir(tenant_id), safe),
                  "wb") as f:
            f.write(data)
        meta = read_json(self._meta_path(tenant_id), {})
        meta[safe] = {"key": key, "size": len(data),
                      "content_type": content_type, "stored_at": utcnow()}
        atomic_write_json(self._meta_path(tenant_id), meta)
        return {"key": key, "size": len(data)}

    def get(self, tenant_id: str, key: str) -> tuple[dict, bytes]:
        safe = sanitize_key(key)
        meta = read_json(self._meta_path(tenant_id), {}).get(safe)
        if meta is None:
            raise KeyError(f"unknown object {key!r}")
        with open(os.path.join(self._tenant_dir(tenant_id), safe), "rb") as f:
            return meta, f.read()

    def delete(self, tenant_id: str, key: str) -> bool:
        safe = sanitize_key(key)
        path = os.path.join(self._tenant_dir(tenant_id), safe)
        if not os.path.exists(path):
            return False
        os.unlink(path)
        meta = read_json(self._meta_path(tenant_id), {})
        meta.pop(safe, None)
        atomic_write_json(self._meta_path(tenant_id), meta)
        return True

    def list_keys(self, tenant_id: str) -> list[str]:
        meta = read_json(self._meta_path(tenant_id), {})
        return [v["key"] for v in meta.values()]

    def tenant_size(self, tenant_id: str) -> int:
        meta = read_json(self._meta_path(tenant_id), {})
        return sum(v["size"] for v in meta.values())
