"""
API-key authentication with scoped keys.

Keys are bearer tokens (omk_...). Only SHA-256 hashes are stored; the raw
key is shown once at creation. Every endpoint declares the scope it needs;
the special scope "admin" grants everything. Auth failures are fail-closed.
"""
from __future__ import annotations

from .models import ApiKeyRecord, _new, hash_key, mint_raw_key


class ApiKeyStore:
    def __init__(self):
        self._by_hash: dict[str, ApiKeyRecord] = {}
        self._by_id: dict[str, ApiKeyRecord] = {}

    def create_key(
        self,
        *,
        name: str,
        scopes: set[str],
        tenant_id: str,
        rate_limit_per_min: int = 60,
    ) -> tuple[ApiKeyRecord, str]:
        raw = mint_raw_key()
        rec = ApiKeyRecord(
            key_id=_new("key"),
            name=name,
            key_hash=hash_key(raw),
            scopes=frozenset(scopes),
            tenant_id=tenant_id,
            rate_limit_per_min=rate_limit_per_min,
        )
        self._by_hash[rec.key_hash] = rec
        self._by_id[rec.key_id] = rec
        return rec, raw  # raw key is returned exactly once

    def authenticate(self, authorization_header: str | None) -> ApiKeyRecord | None:
        """Return the key record for a valid bearer token, else None."""
        if not authorization_header:
            return None
        scheme, _, token = authorization_header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return None
        rec = self._by_hash.get(hash_key(token.strip()))
        if rec is None or rec.revoked:
            return None
        return rec

    def revoke(self, key_id: str) -> bool:
        rec = self._by_id.get(key_id)
        if rec is None:
            return False
        rec.revoked = True
        return True


def has_scope(rec: ApiKeyRecord, required: str) -> bool:
    return "admin" in rec.scopes or required in rec.scopes
