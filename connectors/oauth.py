"""Mock OAuth 2.0 PKCE flow — deterministic and fully offline.

Mirrors the blueprint's OAuth lifecycle:
  1. Client asks to connect a named provider.
  2. Server creates PKCE verifier and state bound to tenant, client,
     scopes, and expiry.
  3. User completes provider authorization in a trusted browser route
     (here: a mock:// URL, so no real network is ever touched).
  4. Callback validates state and exchanges the code server-side.
  5. Refresh token is stored in the vault under an opaque reference.
  6. Connection status reports capabilities, never token contents.
  7. Scope expansion requires a NEW ceremony (new state).
  8. Disconnect revokes where supported and deletes vault material.

Production swaps the token endpoint for the real provider; the ceremony
shape (state binding, server-side exchange, vault storage) is unchanged.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from connectors.models import ConnectorError
from connectors.vault import Vault


@dataclass
class AuthorizationRequest:
    state: str
    tenant_id: str
    provider: str
    scopes: list[str]
    account_label: str
    code_verifier: str          # server-side only; never leaves this object
    expires_at: datetime
    authorization_url: str
    consumed: bool = False


class OAuthFlow:
    def __init__(self, *, state_ttl_minutes: int = 10,
                 now=None):
        self._pending: dict[str, AuthorizationRequest] = {}
        self._state_ttl = timedelta(minutes=state_ttl_minutes)
        self._now = now or (lambda: datetime.now(timezone.utc))

    # -- step 2: begin ceremony -------------------------------------------------
    def start(self, *, tenant_id: str, provider: str, scopes: list[str],
              account_label: str = "") -> AuthorizationRequest:
        for s in scopes:
            if not s or not isinstance(s, str):
                raise ConnectorError("INVALID_SCOPE", "scopes must be non-empty strings")
        state = "st_" + secrets.token_hex(12)
        verifier = secrets.token_urlsafe(48)
        challenge = hashlib.sha256(verifier.encode("ascii")).hexdigest()
        req = AuthorizationRequest(
            state=state, tenant_id=tenant_id, provider=provider,
            scopes=list(scopes), account_label=account_label,
            code_verifier=verifier,
            expires_at=self._now() + self._state_ttl,
            authorization_url=(
                f"mock://oauth/{provider}/authorize"
                f"?state={state}&code_challenge={challenge[:16]}…"
            ),
        )
        self._pending[state] = req
        return req

    # -- step 4: validate + server-side exchange --------------------------------
    def complete(self, *, state: str, code: str, tenant_id: str,
                 vault: Vault) -> tuple[str, str, str]:
        """Validate the callback and exchange the code server-side.

        Returns (access_ref, refresh_ref, principal_hint). Raw token material
        is stored as TWO vault entries (access and refresh separately, so the
        leak scanner can catch either); only opaque references leave here.
        """
        req = self._pending.get(state)
        if req is None:
            raise ConnectorError("UNKNOWN_OAUTH_STATE",
                                 "No pending authorization matches that state.")
        if req.consumed:
            raise ConnectorError("OAUTH_STATE_REUSED",
                                 "This authorization was already completed.")
        if req.tenant_id != tenant_id:
            raise ConnectorError("OAUTH_TENANT_MISMATCH",
                                 "Authorization state is bound to another tenant.")
        if self._now() > req.expires_at:
            del self._pending[state]
            raise ConnectorError("OAUTH_STATE_EXPIRED",
                                 "Authorization expired; start a new ceremony.")
        if not code or not code.startswith("mock-code-"):
            # A real deployment validates the code at the provider token
            # endpoint; here the code is issued by the mock route.
            raise ConnectorError("INVALID_AUTH_CODE",
                                 "Authorization code was rejected.")
        req.consumed = True
        # Server-side exchange (mock): mint token material and vault it as
        # two separate entries — access and refresh are independent secrets.
        access_ref = vault.put(
            tenant_id=tenant_id, provider=req.provider,
            account_label=req.account_label or "oauth",
            purpose="oauth-access:" + ",".join(sorted(req.scopes)),
            secret_value="mock-oauth." + secrets.token_hex(24),
        )
        refresh_ref = vault.put(
            tenant_id=tenant_id, provider=req.provider,
            account_label=req.account_label or "oauth",
            purpose="oauth-refresh:" + ",".join(sorted(req.scopes)),
            secret_value="mock-refresh." + secrets.token_hex(24),
        )
        principal_hint = "o***@mock.example"
        del self._pending[state]
        return access_ref, refresh_ref, principal_hint

    def pending_count(self) -> int:
        return len(self._pending)
