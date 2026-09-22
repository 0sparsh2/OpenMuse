"""Strong-auth approvals for financial/security actions (Phase 10).

R4 (financial/legal/security) approvals require a second confirmation
factor beyond the bound grant: the approval request must carry a
strong-auth attestation bound to the exact request id before a grant may
be issued. The demo harness ships MockStrongAuthenticator (a simulated
second device); production deployments plug a real WebAuthn/TOTP verifier
behind the same interface.

Flow:
  1. ApprovalService.create_request(...) for an R4 tool.
  2. StrongAuthService.challenge(request_id, tenant_id) -> challenge.
  3. User completes the challenge out of band; the authenticator returns
     an Attestation bound to (challenge_id, request_id, tenant_id).
  4. StrongAuthService.verify(request_id, attestation) -> True/False.
  5. Only then may ApprovalService.resolve(..., "approved") proceed —
     the demo's guarded_resolve enforces this ordering.
"""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class StrongAuthChallenge:
    challenge_id: str
    request_id: str
    tenant_id: str
    issued_at: float = field(default_factory=time.time)
    expires_in_seconds: int = 300


@dataclass
class Attestation:
    challenge_id: str
    request_id: str
    tenant_id: str
    factor: str            # e.g. "mock_totp", "webauthn", "totp"
    signature: str         # HMAC over (challenge_id, request_id, tenant_id, factor)
    used: bool = False


class StrongAuthError(ValueError):
    pass


class StrongAuthService:
    """Issues challenges and verifies attestations. The signing key lives
    server-side; the client never sees it."""

    def __init__(self, signing_key: bytes):
        if not signing_key:
            raise ValueError("signing_key is required")
        self._key = signing_key
        self._challenges: dict[str, StrongAuthChallenge] = {}
        self._attested: dict[str, Attestation] = {}

    # -- policy hook -------------------------------------------------------
    @staticmethod
    def required_for(risk: str, tool_name: str = "") -> bool:
        """Strong auth is required for R4 financial/security actions."""
        return risk == "R4"

    # -- challenge lifecycle -------------------------------------------------
    def challenge(self, request_id: str, tenant_id: str) -> StrongAuthChallenge:
        ch = StrongAuthChallenge(
            challenge_id="sac_" + uuid.uuid4().hex[:12],
            request_id=request_id, tenant_id=tenant_id,
        )
        self._challenges[ch.challenge_id] = ch
        return ch

    def _sign(self, challenge_id: str, request_id: str, tenant_id: str, factor: str) -> str:
        msg = f"{challenge_id}||{request_id}||{tenant_id}||{factor}".encode()
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    def verify(self, attestation: Attestation) -> bool:
        """Single-use, expiry-checked, request-bound verification."""
        ch = self._challenges.get(attestation.challenge_id)
        if ch is None:
            return False
        if attestation.used:
            return False
        if time.time() > ch.issued_at + ch.expires_in_seconds:
            return False
        if (attestation.request_id != ch.request_id
                or attestation.tenant_id != ch.tenant_id):
            return False
        expected = self._sign(
            attestation.challenge_id, attestation.request_id,
            attestation.tenant_id, attestation.factor,
        )
        if not hmac.compare_digest(expected, attestation.signature):
            return False
        attestation.used = True
        self._attested[attestation.request_id] = attestation
        return True

    def is_attested(self, request_id: str) -> bool:
        return request_id in self._attested

    def guarded_resolve(self, approvals, request_id: str, decision: str, **kwargs):
        """Resolve an approval only if strong-auth requirements are met.

        Raises StrongAuthError when an R4 request is approved without a
        valid attestation. Denials always pass through.
        """
        req = approvals.requests[request_id]
        if (decision == "approved"
                and self.required_for(req.risk, req.tool_name)
                and not self.is_attested(request_id)):
            raise StrongAuthError(
                f"approval {request_id} ({req.tool_name}, {req.risk}) requires "
                "strong-auth attestation before it can be granted"
            )
        return approvals.resolve(request_id, decision, **kwargs)


class MockStrongAuthenticator:
    """Demo/test authenticator: simulates the user confirming on a second
    device. NEVER use in production — it holds the signing key client-side
    purely so the offline demo can complete the ceremony."""

    def __init__(self, signing_key: bytes, factor: str = "mock_totp"):
        self._key = signing_key
        self.factor = factor

    def complete(self, challenge: StrongAuthChallenge) -> Attestation:
        msg = (f"{challenge.challenge_id}||{challenge.request_id}||"
               f"{challenge.tenant_id}||{self.factor}".encode())
        return Attestation(
            challenge_id=challenge.challenge_id,
            request_id=challenge.request_id,
            tenant_id=challenge.tenant_id,
            factor=self.factor,
            signature=hmac.new(self._key, msg, hashlib.sha256).hexdigest(),
        )
