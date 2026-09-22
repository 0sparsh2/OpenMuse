"""
Idempotency for mutating API calls.

A client-supplied Idempotency-Key makes retries safe: the first request with
a key executes and its response is stored; a repeat with the same key and
the same request fingerprint replays the stored response without
re-executing. The same key with a *different* fingerprint is rejected with
IDEMPOTENCY_KEY_REUSED (the client must mint a fresh key for a new intent).

Scope: per (api key, method, path). Fingerprints are SHA-256 over the
canonical request body.
"""
from __future__ import annotations

import hashlib
import json
import time


def fingerprint_body(body: bytes) -> str:
    try:
        canonical = json.dumps(
            json.loads(body.decode("utf-8") or "{}"),
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
    except (ValueError, UnicodeDecodeError):
        canonical = body.decode("utf-8", "replace")
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyStore:
    def __init__(self):
        # (key_id, method, path, idempotency_key) -> record
        self._records: dict[tuple, dict] = {}

    def lookup(self, *, key_id: str, method: str, path: str, idem_key: str,
               fingerprint: str) -> dict | None:
        """Return stored response to replay, raise on fingerprint mismatch."""
        rec = self._records.get((key_id, method, path, idem_key))
        if rec is None:
            return None
        if rec["fingerprint"] != fingerprint:
            raise KeyReuseError(idem_key)
        return {"status": rec["status"], "body": rec["body"]}

    def store(self, *, key_id: str, method: str, path: str, idem_key: str,
              fingerprint: str, status: int, body: dict) -> None:
        self._records[(key_id, method, path, idem_key)] = {
            "fingerprint": fingerprint,
            "status": status,
            "body": body,
            "stored_at": time.time(),
        }


class KeyReuseError(Exception):
    def __init__(self, idem_key: str):
        super().__init__(f"Idempotency-Key {idem_key!r} was already used with a different body.")
        self.idem_key = idem_key
