"""Connector data model — mirrors the blueprint's connection record and adds
the connector SDK contract (manifest + declared operations)."""
from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Provider error taxonomy (blueprint "Provider safety stops")
# ---------------------------------------------------------------------------
class ProviderErrorCode:
    TRANSIENT = "TRANSIENT"                    # retryable (429 w/ budget, 5xx)
    RATE_LIMIT = "RATE_LIMIT"                  # throttled, may retry later
    RATE_LIMIT_TERMINAL = "RATE_LIMIT_TERMINAL"  # hard stop for account/run
    AUTH_FAILURE = "AUTH_FAILURE"              # bad/expired credential
    NOT_FOUND = "NOT_FOUND"
    FORBIDDEN = "FORBIDDEN"
    INVALID_REQUEST = "INVALID_REQUEST"


class ProviderError(Exception):
    """A classified provider failure. Carries only safe, model-visible text."""

    def __init__(self, code: str, safe_message: str, *, retryable: bool = False):
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable


class ConnectorError(Exception):
    """Connector-framework failure (unknown op, scope, revoked, stopped...)."""

    def __init__(self, code: str, safe_message: str):
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


# ---------------------------------------------------------------------------
# Connector SDK contract
# ---------------------------------------------------------------------------
@dataclass
class OperationDef:
    """One declared operation on a connector — capability and risk up front."""
    name: str                    # "get_repo"
    description: str
    risk: str                    # R0..R4 (R5 never declared)
    side_effect: str             # none | local_write | external_write | destructive
    scopes_required: list[str]   # connector scopes the call needs
    input_schema: dict = field(default_factory=lambda: {"type": "object"})
    output_schema: dict = field(default_factory=lambda: {"type": "object"})
    idempotency: str = "keyed"   # pure | keyed | unsafe_retry


@dataclass
class ConnectorManifest:
    """Declared capabilities of one connector. The registry enforces it."""
    name: str                    # "github" — becomes the tool prefix connector.<name>.
    version: str
    display_name: str
    auth_kinds: list[str]        # subset of {"oauth_pkce", "api_key"}
    scopes: list[str]            # every scope this connector can request
    operations: list[OperationDef] = field(default_factory=list)

    def op(self, name: str) -> OperationDef:
        for op in self.operations:
            if op.name == name:
                return op
        raise ConnectorError("UNKNOWN_OPERATION",
                             f"{self.name} declares no operation {name!r}.")


# ---------------------------------------------------------------------------
# Connection record (blueprint "Connection record")
# ---------------------------------------------------------------------------
@dataclass
class ConnectionRecord:
    connection_id: str
    tenant_id: str
    provider: str
    account_label: str
    principal_hint: str          # redacted hint, e.g. "o***@example.com" — never the raw id
    granted_scopes: list[str] = field(default_factory=list)
    credential_ref: str = ""     # vault://... — NEVER returned to the model
    status: str = "healthy"      # healthy | pending | revoked | error
    last_checked_at: str = ""
    metadata: dict = field(default_factory=dict)

    def public_view(self) -> dict:
        """What status tools may return. credential_ref is deliberately absent."""
        return {
            "connection_id": self.connection_id,
            "tenant_id": self.tenant_id,
            "provider": self.provider,
            "account_label": self.account_label,
            "principal_hint": self.principal_hint,
            "granted_scopes": list(self.granted_scopes),
            "status": self.status,
            "last_checked_at": self.last_checked_at,
            "metadata": dict(self.metadata),
        }
