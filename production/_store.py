"""Shared durable-state helpers for the production package — Phase 8.

Same conventions as the scheduler store: JSON files written atomically
(tmp file + rename), per-tenant directory layout, UTC ISO timestamps.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
from datetime import datetime, timezone


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def sanitize_key(key: str) -> str:
    """Confine an object key to a single path segment."""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in key)
    return safe.strip("._") or "unnamed"
