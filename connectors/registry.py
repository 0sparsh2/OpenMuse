"""Connector registry — the only component that touches credential material.

The registry:
  - holds declared connector manifests and enforces them (unknown ops and
    ungranted scopes are refused before any provider call),
  - runs the OAuth PKCE and API-key-capture connection flows,
  - resolves vault references into short-lived ConnectorHandles scoped to
    exactly one call — tool executors and the model only ever see
    connection ids,
  - scans adapter output for secret leaks and fails closed,
  - enforces the terminal rate-limit hard stop per (run, connection),
  - revokes and deletes vault material on disconnect,
  - emits immutable observability events for every connector lifecycle step.

Scope expansion always requires a new consent ceremony: request_scope_expansion
starts a fresh authorization; the existing connection's scopes are unchanged
until the new ceremony completes.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from connectors.models import (
    ConnectionRecord, ConnectorError, ConnectorManifest,
    ProviderError, ProviderErrorCode as C,
)
from connectors.oauth import OAuthFlow
from connectors.rest import RESTConnector
from connectors.vault import Vault


@dataclass
class ConnectorHandle:
    """Short-lived credential handle, scoped to ONE call. Never leaves the
    registry/adapter boundary; never serialized into events or results."""
    credential: str
    connection_id: str
    provider: str


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConnectorRegistry:
    def __init__(self, *, vault: Vault, oauth: OAuthFlow | None = None,
                 event_log=None):
        self.vault = vault
        self.oauth = oauth or OAuthFlow()
        self._adapters: dict[str, RESTConnector] = {}
        self._connections: dict[str, ConnectionRecord] = {}
        self._refresh_refs: dict[str, str] = {}  # connection_id -> vault ref (never model-visible)
        self._stops: set[tuple[str, str]] = set()  # (run_id, connection_id)
        self._log = event_log

    # -- SDK: declare connectors ------------------------------------------------
    def register_connector(self, adapter: RESTConnector) -> None:
        manifest: ConnectorManifest = adapter.manifest
        if manifest.name in self._adapters:
            raise ValueError(f"connector {manifest.name!r} already registered")
        for op in manifest.operations:
            if op.risk == "R5":
                raise ValueError(f"operation {op.name} may not declare R5")
            unknown = [s for s in op.scopes_required if s not in manifest.scopes]
            if unknown:
                raise ValueError(f"operation {op.name} requires undeclared scopes {unknown}")
        self._adapters[manifest.name] = adapter
        self._emit("connector.registered",
                   {"provider": manifest.name, "version": manifest.version,
                    "operations": [o.name for o in manifest.operations]})

    def manifest(self, provider: str) -> ConnectorManifest:
        try:
            return self._adapters[provider].manifest
        except KeyError:
            raise ConnectorError("UNKNOWN_PROVIDER",
                                 f"No connector registered for {provider!r}.") from None

    # -- connection flows --------------------------------------------------------
    def connect(self, *, tenant_id: str, provider: str, auth_kind: str,
                scopes: list[str], account_label: str = "",
                capture_id: str | None = None) -> dict:
        """Begin connecting a provider.

        oauth_pkce -> returns {authorization_url, state}; the user completes
        authorization, then complete_authorization() finishes the ceremony.
        api_key    -> consumes a completed Secure Vault capture (capture_id);
                     the raw key never passes through this call's arguments.
        """
        manifest = self.manifest(provider)
        if auth_kind not in manifest.auth_kinds:
            raise ConnectorError("AUTH_KIND_UNSUPPORTED",
                                 f"{provider} does not support {auth_kind}.")
        unknown = [s for s in scopes if s not in manifest.scopes]
        if unknown:
            raise ConnectorError("UNKNOWN_SCOPE",
                                 f"{provider} declares no scopes {unknown}.")
        if auth_kind == "oauth_pkce":
            req = self.oauth.start(tenant_id=tenant_id, provider=provider,
                                   scopes=scopes, account_label=account_label)
            conn = self._new_connection(
                tenant_id=tenant_id, provider=provider,
                account_label=account_label, scopes=scopes, status="pending")
            conn.metadata["oauth_state"] = req.state
            self._emit("connector.auth_started",
                       {"connection_id": conn.connection_id,
                        "provider": provider, "scopes": scopes})
            return {"connection_id": conn.connection_id,
                    "authorization_url": req.authorization_url,
                    "state": req.state,
                    "status": "pending",
                    "next": "complete_authorization with the provider code"}
        # api_key via Secure Vault capture — the model never handles the key.
        if not capture_id:
            raise ConnectorError("CAPTURE_REQUIRED",
                                 "API-key connect needs a completed vault capture.")
        credential_ref = self.vault.consume_capture(capture_id, tenant_id=tenant_id)
        conn = self._new_connection(
            tenant_id=tenant_id, provider=provider, account_label=account_label,
            scopes=scopes, status="healthy", credential_ref=credential_ref,
            principal_hint="k*** (api key)")
        self._emit("connector.connected",
                   {"connection_id": conn.connection_id,
                    "provider": provider, "scopes": scopes,
                    "auth_kind": "api_key"})
        return {"connection_id": conn.connection_id, "status": "healthy"}

    def complete_authorization(self, *, tenant_id: str, state: str,
                               code: str) -> dict:
        access_ref, refresh_ref, principal_hint = self.oauth.complete(
            state=state, code=code, tenant_id=tenant_id, vault=self.vault)
        target, expansion = self._target_for_state(tenant_id, state)
        # Rotate in the fresh access material; the old refresh entry is
        # replaced too (a completed ceremony supersedes the old grant).
        old_access, old_refresh = target.credential_ref, self._refresh_refs.get(target.connection_id)
        target.credential_ref = access_ref
        self._refresh_refs[target.connection_id] = refresh_ref
        for ref in (old_access, old_refresh):
            if ref:
                self.vault.delete(ref)
        if expansion:
            new_scopes = sorted(set(target.granted_scopes) | set(expansion["scopes"]))
            target.granted_scopes = new_scopes
            del target.metadata["pending_expansion"]
            self._emit("connector.scopes_expanded",
                       {"connection_id": target.connection_id,
                        "granted_scopes": new_scopes})
        else:
            target.principal_hint = principal_hint
            target.status = "healthy"
            target.metadata.pop("oauth_state", None)
            self._emit("connector.connected",
                       {"connection_id": target.connection_id,
                        "provider": target.provider,
                        "scopes": target.granted_scopes,
                        "auth_kind": "oauth_pkce"})
        target.last_checked_at = _utcnow()
        return {"connection_id": target.connection_id,
                "status": target.status,
                "granted_scopes": list(target.granted_scopes)}

    def request_scope_expansion(self, *, tenant_id: str,
                                connection_id: str,
                                new_scopes: list[str]) -> dict:
        """Scope expansion requires a NEW consent ceremony. The existing
        connection keeps its current scopes until the ceremony completes."""
        conn = self._get(tenant_id, connection_id)
        manifest = self.manifest(conn.provider)
        unknown = [s for s in new_scopes if s not in manifest.scopes]
        if unknown:
            raise ConnectorError("UNKNOWN_SCOPE",
                                 f"{conn.provider} declares no scopes {unknown}.")
        if set(new_scopes) <= set(conn.granted_scopes):
            return {"connection_id": connection_id,
                    "status": conn.status,
                    "note": "scopes already granted; no new ceremony needed"}
        req = self.oauth.start(tenant_id=tenant_id, provider=conn.provider,
                               scopes=new_scopes,
                               account_label=conn.account_label)
        conn.metadata["pending_expansion"] = {
            "state": req.state, "scopes": list(new_scopes)}
        self._emit("connector.scope_expansion_started",
                   {"connection_id": connection_id,
                    "requested_scopes": new_scopes})
        return {"connection_id": connection_id,
                "authorization_url": req.authorization_url,
                "state": req.state,
                "note": "complete_authorization to grant the expanded scopes"}

    # -- status / disconnect ------------------------------------------------------
    def status(self, *, tenant_id: str, connection_id: str) -> dict:
        conn = self._get(tenant_id, connection_id)
        conn.last_checked_at = _utcnow()
        view = conn.public_view()
        manifest = self.manifest(conn.provider)
        view["operations"] = [
            {"name": op.name, "description": op.description, "risk": op.risk,
             "scopes_required": op.scopes_required}
            for op in manifest.operations
        ]
        return view

    def disconnect(self, *, tenant_id: str, connection_id: str) -> dict:
        """Revoke provider tokens where supported and delete vault material."""
        conn = self._get(tenant_id, connection_id)
        # Best-effort provider revocation (mock: no network; a real adapter
        # would call the provider revocation endpoint here).
        refs = [conn.credential_ref, self._refresh_refs.pop(connection_id, "")]
        for ref in refs:
            if ref:
                self.vault.delete(ref)
        conn.credential_ref = ""
        conn.status = "revoked"
        conn.last_checked_at = _utcnow()
        self._emit("connector.disconnected",
                   {"connection_id": connection_id, "provider": conn.provider})
        return {"connection_id": connection_id, "status": "revoked"}

    # -- the single call path -------------------------------------------------------
    def call(self, *, run_id: str, tenant_id: str, connection_id: str,
             op: str, args: dict) -> dict:
        """Execute one declared operation. Raises ConnectorError/ ProviderError."""
        if (run_id, connection_id) in self._stops:
            raise ConnectorError(
                "PROVIDER_HARD_STOP",
                "This provider hit a terminal limit for this run; no further "
                "calls will be attempted. Report partial progress instead.")
        conn = self._get(tenant_id, connection_id)
        if conn.status != "healthy":
            raise ConnectorError("CONNECTION_NOT_HEALTHY",
                                 f"Connection is {conn.status}; reconnect first.")
        adapter = self._adapters[conn.provider]
        opdef = adapter.manifest.op(op)  # unknown op -> UNKNOWN_OPERATION
        missing = [s for s in opdef.scopes_required
                   if s not in conn.granted_scopes]
        if missing:
            raise ConnectorError(
                "SCOPE_NOT_GRANTED",
                f"Operation {op!r} needs scopes {missing}; the connection "
                "grants only a subset. Scope expansion needs a new consent "
                "ceremony.")
        handle = ConnectorHandle(
            credential=self.vault._resolve(conn.credential_ref,
                                           tenant_id=tenant_id),
            connection_id=connection_id, provider=conn.provider)
        self._emit("connector.call_started",
                   {"connection_id": connection_id, "op": op,
                    "run_id": run_id})
        try:
            output = adapter.execute(op, handle.credential, dict(args))
        except ProviderError as exc:
            self._emit("connector.provider_error",
                       {"connection_id": connection_id, "op": op,
                        "code": exc.code})
            if exc.code == C.RATE_LIMIT_TERMINAL:
                # Hard stop: no retries, no alternate endpoints/workers.
                self._stops.add((run_id, connection_id))
                self._emit("connector.hard_stop",
                           {"connection_id": connection_id, "run_id": run_id,
                            "reason": "RATE_LIMIT_TERMINAL"})
            raise
        # Secrets hygiene: verify adapter output before it reaches the model.
        serialized = json.dumps(output, ensure_ascii=False, default=str)
        cleaned, leaked = self.vault.scan(serialized)
        if leaked:
            self._emit("connector.secret_leak_blocked",
                       {"connection_id": connection_id, "op": op})
            raise ConnectorError(
                "SECRET_LEAK_BLOCKED",
                "The provider response contained credential material and was "
                "blocked before reaching the model.")
        self._emit("connector.call_succeeded",
                   {"connection_id": connection_id, "op": op})
        return output

    # -- internals ------------------------------------------------------------------
    def _new_connection(self, *, tenant_id: str, provider: str,
                        account_label: str, scopes: list[str], status: str,
                        credential_ref: str = "",
                        principal_hint: str = "") -> ConnectionRecord:
        conn = ConnectionRecord(
            connection_id="con_" + uuid.uuid4().hex[:12],
            tenant_id=tenant_id, provider=provider,
            account_label=account_label or provider,
            principal_hint=principal_hint,
            granted_scopes=list(scopes), credential_ref=credential_ref,
            status=status, last_checked_at=_utcnow(), metadata={})
        self._connections[conn.connection_id] = conn
        return conn

    def _get(self, tenant_id: str, connection_id: str) -> ConnectionRecord:
        conn = self._connections.get(connection_id)
        if conn is None or conn.tenant_id != tenant_id:
            raise ConnectorError("UNKNOWN_CONNECTION",
                                 "No such connection for this tenant.")
        return conn

    def _target_for_state(self, tenant_id: str, state: str):
        """Bind a completed ceremony to its connection: either the pending
        connection that started it, or a healthy connection with a pending
        scope-expansion ceremony. Returns (connection, expansion_or_None)."""
        for conn in self._connections.values():
            if conn.tenant_id != tenant_id:
                continue
            if (conn.status == "pending"
                    and conn.metadata.get("oauth_state") == state):
                return conn, None
            expansion = conn.metadata.get("pending_expansion")
            if expansion and expansion.get("state") == state:
                return conn, expansion
        raise ConnectorError("UNKNOWN_CONNECTION",
                             "No pending connection for this authorization.")

    def _emit(self, event_type: str, payload: dict) -> None:
        if self._log is not None:
            self._log.append(event_type, payload)
