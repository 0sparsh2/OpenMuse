"""Durable run-record store (the "database" tier) — Phase 8.

Append-only per-tenant JSONL of run lifecycle records: the offline
substitute for the database tier that run state lives in. Recovery replays
this log; tenant deletion purges it.
"""
from __future__ import annotations

import json
import os

from ._store import utcnow
from .models import new_id


class RunLogStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, tenant_id: str) -> str:
        return os.path.join(self.root, f"{tenant_id}.jsonl")

    def append(self, tenant_id: str, record: dict) -> dict:
        entry = {"record_id": new_id("rec"), "tenant_id": tenant_id,
                 "at": utcnow(), **record}
        with open(self._path(tenant_id), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def list(self, tenant_id: str) -> list[dict]:
        path = self._path(tenant_id)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def count(self, tenant_id: str) -> int:
        return len(self.list(tenant_id))

    def purge(self, tenant_id: str) -> int:
        n = self.count(tenant_id)
        path = self._path(tenant_id)
        if os.path.exists(path):
            os.unlink(path)
        return n
