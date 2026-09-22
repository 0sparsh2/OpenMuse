"""Protected one-time-code path (blueprint "Secrets hygiene").

One-time codes (SMS/email sign-in codes, authenticator codes) use a protected
read-and-fill route: the code is delivered to the USER out of band, and the
agent receives only success/failure — never the raw code.

The `deliver` callback models the out-of-band channel (push notification,
SMS). In production it is a real delivery; here it is injectable so tests
can play the user without the agent ever seeing the code.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class _Challenge:
    code: str
    connection_id: str
    expires_at: float
    attempts: int = 0


class OneTimeCodeService:
    def __init__(self, *, ttl_seconds: int = 300, max_attempts: int = 5):
        self._challenges: dict[str, _Challenge] = {}
        self._ttl = ttl_seconds
        self._max_attempts = max_attempts

    def issue(self, connection_id: str,
              deliver: Callable[[str], None]) -> dict:
        """Start a code challenge. `deliver` receives the raw code OUT OF
        BAND (the user's device). The returned dict contains no code."""
        code = f"{secrets.randbelow(900_000) + 100_000:06d}"
        code_id = "otp_" + secrets.token_hex(8)
        self._challenges[code_id] = _Challenge(
            code=code, connection_id=connection_id,
            expires_at=time.time() + self._ttl)
        deliver(code)  # out-of-band: the agent never sees this value
        return {"code_id": code_id, "connection_id": connection_id,
                "status": "code_sent_to_user"}

    def fill(self, code_id: str, code: str, *, connection_id: str) -> dict:
        """Verify a user-supplied code. Returns only success/failure."""
        ch = self._challenges.get(code_id)
        if ch is None:
            return {"ok": False, "reason": "unknown_or_expired_challenge"}
        if ch.connection_id != connection_id:
            return {"ok": False, "reason": "connection_mismatch"}
        if time.time() > ch.expires_at:
            del self._challenges[code_id]
            return {"ok": False, "reason": "expired"}
        ch.attempts += 1
        if ch.attempts > self._max_attempts:
            del self._challenges[code_id]
            return {"ok": False, "reason": "too_many_attempts"}
        if secrets.compare_digest(code, ch.code):
            del self._challenges[code_id]
            return {"ok": True}
        return {"ok": False, "reason": "incorrect_code"}
