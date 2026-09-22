"""
API domain records: sessions, artifacts, webhooks, API keys.

Durable state lives in ApiBackend; these are the record types. Secrets are
never stored here — API keys are kept as SHA-256 hashes, webhook signing
secrets are referenced opaquely by the delivery layer.
"""
from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass, field


def _new(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


@dataclass
class ApiKeyRecord:
    key_id: str
    name: str
    key_hash: str                    # sha256 of the raw key; raw key never stored
    scopes: frozenset                # e.g. {"runs:write", "approvals:decide"}
    tenant_id: str
    rate_limit_per_min: int = 60
    created_at: float = field(default_factory=time.time)
    revoked: bool = False

    def public_view(self) -> dict:
        return {
            "key_id": self.key_id,
            "name": self.name,
            "scopes": sorted(self.scopes),
            "tenant_id": self.tenant_id,
            "rate_limit_per_min": self.rate_limit_per_min,
            "revoked": self.revoked,
        }


def mint_raw_key() -> str:
    return "omk_" + secrets.token_urlsafe(32)


def hash_key(raw: str) -> str:
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class SessionRecord:
    session_id: str
    chat_id: str                     # blueprint-canonical chat identifier
    tenant_id: str
    user_id: str
    title: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class ArtifactRecord:
    artifact_id: str
    tenant_id: str
    name: str
    content_type: str
    size: int
    sha256: str
    created_at: float = field(default_factory=time.time)


@dataclass
class WebhookSubscription:
    id: str
    tenant_id: str
    url: str
    events: list                    # subset of {"run.completed", "run.failed"}
    secret_ref: str                  # opaque ref; raw secret held by the vault seam
    active: bool = True
    created_at: float = field(default_factory=time.time)


# Canonical SSE event types emitted per run.
SSE_RUN_STATUS = "run.status"
SSE_ASSISTANT_DELTA = "assistant.delta"
SSE_APPROVAL_REQUIRED = "approval.required"
SSE_APPROVAL_DECIDED = "approval.decided"
SSE_TOOL_RESULT = "tool.result"
SSE_RUN_COMPLETED = "run.completed"
SSE_RUN_FAILED = "run.failed"
SSE_RUN_CANCELLED = "run.cancelled"
