"""
Saved logins for the live browser (issue #16).

Users store site logins here; the agent only ever sees an opaque reference
(`vault://login/<id>#username` / `#password`). The browser worker resolves
the reference at the moment of typing, and only when:
  - the user approved that exact sign-in (the call carries a bound grant), and
  - the page's origin matches the login's site (no filling on look-alikes).

Secrets are encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256). The key
comes from OPENMUSE_VAULT_KEY or is generated once into a 0600 key file.
Passwords are never returned by the API, never logged, never put in model
context, events, observations or screenshots (password inputs render masked).
"""
from __future__ import annotations

import os
import re
import time
import uuid
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

REF_RE = re.compile(r"^vault://login/(lg_[a-f0-9]{12})#(username|password)$")


def site_origin(url: str) -> str:
    """Normalise to scheme://host (www. dropped) for comparisons."""
    url = (url or "").strip()
    if not re.match(r"^[a-z]+://", url):
        url = "https://" + url
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    scheme = p.scheme or "https"
    try:
        port = p.port
    except ValueError:
        return ""
    if port and port != {"http": 80, "https": 443}.get(scheme):
        host = f"{host}:{port}"
    return f"{scheme}://{host}" if host else ""


class LoginVault:
    def __init__(self, backend, *, key: str = "", key_file: str = ""):
        self.backend = backend
        if not key and key_file:
            if os.path.exists(key_file):
                with open(key_file, "rb") as fh:
                    key = fh.read().decode().strip()
            else:
                key = Fernet.generate_key().decode()
                os.makedirs(os.path.dirname(key_file) or ".", exist_ok=True)
                with open(key_file, "w") as fh:
                    fh.write(key)
                os.chmod(key_file, 0o600)
        self._f = Fernet(key.encode() if isinstance(key, str) else key)
        self._mem: dict[str, dict] = {}

    # -- storage ----------------------------------------------------------------
    def _put(self, rec: dict) -> None:
        if self.backend.db is not None:
            self.backend.db.kv_put("logins", rec["login_id"], rec, user_id=rec["user_id"])
        else:
            self._mem[rec["login_id"]] = rec

    def _get(self, login_id: str) -> dict | None:
        return self.backend.db.kv_get("logins", login_id) if self.backend.db is not None else self._mem.get(login_id)

    def add(self, user_id: str, *, site: str, username: str, password: str, label: str = "") -> dict:
        origin = site_origin(site)
        if not origin or not (username or "").strip() or not password:
            raise ValueError("a login needs a site, a username and a password")
        rec = {"login_id": "lg_" + uuid.uuid4().hex[:12], "user_id": user_id, "origin": origin,
               "username": username.strip()[:200], "label": (label or "").strip()[:80],
               "secret": self._f.encrypt(password.encode("utf-8")).decode(), "created_at": time.time(),
               "last_used": None}
        self._put(rec)
        return self.public(rec)

    def list(self, user_id: str) -> list[dict]:
        items = (self.backend.db.kv_list("logins", user_id=user_id, limit=500) if self.backend.db is not None
                 else [r for r in self._mem.values() if r["user_id"] == user_id])
        return [self.public(r) for r in sorted(items, key=lambda r: r["origin"])]

    def delete(self, user_id: str, login_id: str) -> bool:
        rec = self._get(login_id)
        if not rec or rec["user_id"] != user_id:
            return False
        if self.backend.db is not None:
            self.backend.db.kv_delete("logins", login_id)
        else:
            self._mem.pop(login_id, None)
        return True

    @staticmethod
    def public(rec: dict) -> dict:
        """Everything but the secret."""
        return {"login_id": rec["login_id"], "ref": f"vault://login/{rec['login_id']}",
                "site": rec["origin"], "username": rec["username"], "label": rec["label"],
                "created_at": rec["created_at"], "last_used": rec.get("last_used")}

    # -- use (browser worker only) --------------------------------------------------
    def info(self, user_id: str, ref: str) -> dict | None:
        """Non-secret facts about a ref (for approval cards)."""
        m = REF_RE.match(ref or "")
        rec = self._get(m.group(1)) if m else None
        if not rec or rec["user_id"] != user_id:
            return None
        return {"site": rec["origin"], "username": rec["username"], "field": m.group(2)}

    def resolve(self, user_id: str, ref: str, page_url: str) -> str:
        m = REF_RE.match(ref or "")
        if not m:
            raise ValueError("malformed credential reference")
        rec = self._get(m.group(1))
        if not rec or rec["user_id"] != user_id:
            raise ValueError("unknown credential reference")
        if site_origin(page_url) != rec["origin"]:
            raise PermissionError(f"this login is for {rec['origin']}, not {site_origin(page_url)}")
        rec["last_used"] = time.time()
        self._put(rec)
        if m.group(2) == "username":
            return rec["username"]
        try:
            return self._f.decrypt(rec["secret"].encode()).decode("utf-8")
        except InvalidToken:
            raise ValueError("stored login can't be decrypted (vault key changed?)")
