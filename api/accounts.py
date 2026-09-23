"""
User accounts: sign up / sign in / sign out.

Every signed-in user gets a scoped bearer session token (an ApiKeyRecord
bound to their user_id), so everything downstream — chats, runs, approvals,
browser sessions, and memory — is attributed to and isolated per user.

- Passwords: scrypt (n=2^14, r=8, p=1) with a per-user salt; only the hash
  is stored. Comparisons are constant-time.
- Brute force: 5 failed sign-ins lock the email for 15 minutes.
- Session tokens: raw token shown once; only its SHA-256 is persisted (0600),
  so sessions survive restarts without storing a usable credential.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from .models import ApiKeyRecord, _new, hash_key, mint_raw_key

USER_SCOPES = frozenset({
    "sessions:write", "sessions:read", "runs:write", "runs:read",
    "approvals:read", "approvals:decide", "artifacts:write", "artifacts:read",
    "memory:read", "memory:write",
})
SESSION_TTL_S = 30 * 24 * 3600
MAX_FAILURES = 5
LOCKOUT_S = 15 * 60
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt,
                          n=2 ** 14, r=8, p=1, dklen=32).hex()


class AccountStore:
    def __init__(self, root: str, keys, tenant_id: str):
        self.root = root
        self.keys = keys
        self.tenant_id = tenant_id
        os.makedirs(root, exist_ok=True)
        self._users_path = os.path.join(root, "users.json")
        self._sessions_path = os.path.join(root, "sessions.json")
        self._lock = threading.Lock()
        self._users: dict[str, dict] = self._read(self._users_path)
        self._sessions: dict[str, dict] = self._read(self._sessions_path)
        self._failures: dict[str, list[float]] = {}
        self._restore_sessions()

    # -- persistence ----------------------------------------------------------
    @staticmethod
    def _read(path: str) -> dict:
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _write(self, path: str, data: dict) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def _restore_sessions(self) -> None:
        now = time.time()
        for key_hash, s in list(self._sessions.items()):
            if s["expires_at"] < now or s["user_id"] not in self._users:
                self._sessions.pop(key_hash)
                continue
            self._register(key_hash, s)
        self._write(self._sessions_path, self._sessions)

    def _register(self, key_hash: str, s: dict) -> ApiKeyRecord:
        rec = ApiKeyRecord(key_id=s["key_id"], name="session:" + s["user_id"],
                           key_hash=key_hash, scopes=USER_SCOPES,
                           tenant_id=self.tenant_id, rate_limit_per_min=3000,
                           created_at=s["created_at"], user_id=s["user_id"])
        self.keys._by_hash[key_hash] = rec
        self.keys._by_id[rec.key_id] = rec
        return rec

    # -- public API -------------------------------------------------------------
    def signup(self, *, email: str, password: str, name: str = "") -> tuple[dict, str]:
        email = (email or "").strip().lower()
        name = (name or "").strip()[:80]
        if not EMAIL_RE.match(email):
            raise AuthError("INVALID_EMAIL", "Enter a valid email address.")
        if len(password or "") < 8:
            raise AuthError("WEAK_PASSWORD", "Password must be at least 8 characters.")
        with self._lock:
            if any(u["email"] == email for u in self._users.values()):
                raise AuthError("EMAIL_TAKEN", "An account with this email already exists.", 409)
            salt = secrets.token_bytes(16)
            user = {"user_id": _new("usr"), "email": email,
                    "name": name or email.split("@")[0],
                    "salt": salt.hex(), "pw_hash": _hash_password(password, salt),
                    "created_at": time.time()}
            self._users[user["user_id"]] = user
            self._write(self._users_path, self._users)
            token = self._start_session(user["user_id"])
        return self.public(user), token

    def login(self, *, email: str, password: str) -> tuple[dict, str]:
        email = (email or "").strip().lower()
        now = time.time()
        with self._lock:
            recent = [t for t in self._failures.get(email, []) if now - t < LOCKOUT_S]
            self._failures[email] = recent
            if len(recent) >= MAX_FAILURES:
                raise AuthError("LOCKED", "Too many failed attempts. Try again in 15 minutes.", 429)
            user = next((u for u in self._users.values() if u["email"] == email), None)
            # hash even for unknown emails so timing doesn't reveal which exist
            salt = bytes.fromhex(user["salt"]) if user else b"\0" * 16
            candidate = _hash_password(password or "", salt)
            if user is None or not hmac.compare_digest(candidate, user["pw_hash"]):
                recent.append(now)
                raise AuthError("BAD_CREDENTIALS", "Email or password is incorrect.", 401)
            self._failures.pop(email, None)
            token = self._start_session(user["user_id"])
        return self.public(user), token

    def logout(self, key_id: str) -> bool:
        with self._lock:
            for key_hash, s in list(self._sessions.items()):
                if s["key_id"] == key_id:
                    self._sessions.pop(key_hash)
                    rec = self.keys._by_hash.pop(key_hash, None)
                    if rec is not None:
                        rec.revoked = True
                        self.keys._by_id.pop(rec.key_id, None)
                    self._write(self._sessions_path, self._sessions)
                    return True
        return False

    def get(self, user_id: str) -> dict | None:
        u = self._users.get(user_id)
        return self.public(u) if u else None

    @staticmethod
    def public(user: dict) -> dict:
        return {"user_id": user["user_id"], "email": user["email"],
                "name": user["name"], "created_at": user["created_at"]}

    def _start_session(self, user_id: str) -> str:
        raw = mint_raw_key()
        s = {"key_id": _new("key"), "user_id": user_id,
             "created_at": time.time(), "expires_at": time.time() + SESSION_TTL_S}
        key_hash = hash_key(raw)
        self._sessions[key_hash] = s
        self._register(key_hash, s)
        self._write(self._sessions_path, self._sessions)
        return raw
