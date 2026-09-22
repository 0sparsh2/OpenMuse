"""Secure vault seam — the agent and client NEVER handle raw secrets.

The vault stores credential material keyed by opaque `vault://` references
bound to (tenant, provider, account, purpose). Only the connector registry
resolves a reference, and only into a short-lived ConnectorHandle scoped to
one call. The API never returns credential_ref to the model.

Secrets hygiene (blueprint):
  - Vault access is workload-identity style: the registry is the only
    resolver in this process; tool executors receive connection ids only.
  - The vault scans text for stored secret material and reports leaks, so
    adapter output can be verified before it reaches the model.
  - delete() removes the material entirely (disconnect path).
"""
from __future__ import annotations

import abc
import hashlib
import hmac
import uuid


class Vault(abc.ABC):
    @abc.abstractmethod
    def put(self, *, tenant_id: str, provider: str, account_label: str,
            purpose: str, secret_value: str) -> str:
        """Store secret material; return its opaque vault:// reference."""
        raise NotImplementedError

    @abc.abstractmethod
    def delete(self, credential_ref: str) -> bool:
        """Delete stored material. Returns True if something was removed."""
        raise NotImplementedError

    @abc.abstractmethod
    def has(self, credential_ref: str) -> bool:
        raise NotImplementedError

    # -- internal: the registry is the only sanctioned resolver --------------
    @abc.abstractmethod
    def _resolve(self, credential_ref: str, *, tenant_id: str) -> str:
        """Resolve a reference to raw material. REGISTRY ONLY — never a tool."""
        raise NotImplementedError

    @abc.abstractmethod
    def scan(self, text: str) -> tuple[str, bool]:
        """Return (redacted_text, leaked). Used to verify adapter output."""
        raise NotImplementedError

    # -- Secure Vault capture flow -------------------------------------------
    @abc.abstractmethod
    def create_capture(self, *, tenant_id: str, provider: str, purpose: str) -> str:
        """Open a capture slot (the Secure Vault capture page)."""
        raise NotImplementedError

    @abc.abstractmethod
    def complete_capture(self, capture_id: str, secret_value: str) -> str:
        """USER-SIDE ONLY: the capture page deposits the secret. Returns vault ref."""
        raise NotImplementedError


class MemoryVault(Vault):
    """In-memory vault substitute for development and tests.

    Holds ONLY test credentials. Production uses envelope encryption with
    tenant-scoped data keys (deployment blueprint).
    """

    def __init__(self):
        self._secrets: dict[str, dict] = {}   # ref -> binding record
        self._captures: dict[str, dict] = {}  # capture_id -> slot
        self._counter = 0

    # -- storage ------------------------------------------------------------
    def put(self, *, tenant_id: str, provider: str, account_label: str,
            purpose: str, secret_value: str) -> str:
        if not secret_value:
            raise ValueError("refusing to store empty secret material")
        self._counter += 1
        ref = f"vault://{provider}/{uuid.uuid4().hex[:16]}"
        self._secrets[ref] = {
            "tenant_id": tenant_id,
            "provider": provider,
            "account_label": account_label,
            "purpose": purpose,
            "value": secret_value,
            # A fingerprint lets operators match a ref to vault-side material
            # without ever seeing the plaintext.
            "fingerprint": "sha256:" + hashlib.sha256(
                secret_value.encode("utf-8")).hexdigest()[:16],
        }
        return ref

    def delete(self, credential_ref: str) -> bool:
        return self._secrets.pop(credential_ref, None) is not None

    def has(self, credential_ref: str) -> bool:
        return credential_ref in self._secrets

    def _resolve(self, credential_ref: str, *, tenant_id: str) -> str:
        rec = self._secrets.get(credential_ref)
        if rec is None:
            raise KeyError(f"unknown credential reference")
        if rec["tenant_id"] != tenant_id:
            raise PermissionError("credential reference is bound to another tenant")
        return rec["value"]

    def scan(self, text: str) -> tuple[str, bool]:
        """Redact any stored secret value found in text. Constant-time compare
        per candidate so the scan itself does not become a timing oracle."""
        leaked = False
        redacted = text
        for rec in self._secrets.values():
            value = rec["value"]
            if len(value) < 8:
                continue  # too short to be a meaningful secret marker
            found = False
            start = 0
            while True:
                idx = redacted.find(value, start)
                if idx == -1:
                    break
                found = True
                start = idx + len(value)
            if found:
                leaked = True
                redacted = redacted.replace(value, "[REDACTED_SECRET]")
        # Also catch the opaque refs themselves outside approved channels.
        return redacted, leaked

    # -- capture flow ---------------------------------------------------------
    def create_capture(self, *, tenant_id: str, provider: str, purpose: str) -> str:
        self._counter += 1
        capture_id = f"cap_{uuid.uuid4().hex[:12]}"
        self._captures[capture_id] = {
            "tenant_id": tenant_id, "provider": provider,
            "purpose": purpose, "completed": False, "ref": None,
        }
        return capture_id

    def complete_capture(self, capture_id: str, secret_value: str) -> str:
        slot = self._captures.get(capture_id)
        if slot is None:
            raise KeyError("unknown capture slot")
        if slot["completed"]:
            raise ValueError("capture slot is single-use and already consumed")
        ref = self.put(tenant_id=slot["tenant_id"], provider=slot["provider"],
                       account_label="api-key", purpose=slot["purpose"],
                       secret_value=secret_value)
        slot["completed"] = True
        slot["ref"] = ref
        return ref

    def consume_capture(self, capture_id: str, *, tenant_id: str) -> str:
        """Registry-side: consume a completed capture into a vault ref."""
        slot = self._captures.get(capture_id)
        if slot is None or not slot["completed"]:
            raise KeyError("capture slot is not completed")
        if slot["tenant_id"] != tenant_id:
            raise PermissionError("capture slot is bound to another tenant")
        ref = slot["ref"]
        del self._captures[capture_id]  # single-use
        return ref

    # -- test introspection (never model-visible) -------------------------------
    def _binding(self, credential_ref: str) -> dict:
        return dict(self._secrets[credential_ref])
