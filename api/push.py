"""
Web Push for installed PWAs (issue #17).

Every notification from the notification center (#5) also goes out as a Web
Push to each of the user's subscribed devices, so an approval request buzzes
a locked phone. Keys are VAPID (from VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY, or
generated once into a 0600 key file). Subscriptions are per user; a push
service answering 404/410 means the device unsubscribed, so we drop it.
Payloads carry only what the lock screen shows (title, a short body, the
notification id) — never message content beyond the notification itself.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from urllib.parse import urlparse

PUSH_HOSTS_SUFFIXES = (".googleapis.com", ".mozilla.com", ".mozaws.net", ".push.apple.com",
                       ".notify.windows.com", ".windows.com")


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _load_or_make_keys(key_file: str) -> tuple[str, str]:
    """(public applicationServerKey b64url, private PEM)."""
    pub, priv = os.environ.get("VAPID_PUBLIC_KEY", ""), os.environ.get("VAPID_PRIVATE_KEY", "")
    if pub and priv:
        return pub, priv.replace("\\n", "\n")
    if key_file and os.path.exists(key_file):
        with open(key_file) as fh:
            data = json.load(fh)
        return data["public"], data["private_pem"]
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    raw = key.public_key().public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
    pub = _b64url(raw)
    if key_file:
        os.makedirs(os.path.dirname(key_file) or ".", exist_ok=True)
        with open(key_file, "w") as fh:
            json.dump({"public": pub, "private_pem": pem}, fh)
        os.chmod(key_file, 0o600)
    return pub, pem


def valid_endpoint(url: str) -> bool:
    """Only real push services (https) — never an arbitrary URL we'd POST to."""
    p = urlparse(url or "")
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not host:
        return False
    if os.environ.get("OPENMUSE_PUSH_ANY_HOST") == "1":  # tests / self-hosted push services
        return True
    return any(host.endswith(s) or host == s.lstrip(".") for s in PUSH_HOSTS_SUFFIXES)


class PushService:
    def __init__(self, backend, *, key_file: str = "", subject: str = "", sender=None):
        self.backend = backend
        self.public_key, self._private_pem = _load_or_make_keys(key_file)
        self.subject = subject or os.environ.get("VAPID_SUBJECT", "mailto:admin@openmuse.local")
        self._sender = sender or self._webpush
        self._mem: dict[str, dict] = {}
        self._lock = threading.Lock()
        backend._note_listeners.append(self._on_note)

    # -- subscriptions ------------------------------------------------------------
    @staticmethod
    def _sid(endpoint: str) -> str:
        return "psh_" + hashlib.sha256(endpoint.encode()).hexdigest()[:20]

    def subscribe(self, user_id: str, sub: dict, *, device: str = "") -> dict:
        endpoint = str((sub or {}).get("endpoint", ""))
        keys = (sub or {}).get("keys") or {}
        if not valid_endpoint(endpoint) or not keys.get("p256dh") or not keys.get("auth"):
            raise ValueError("not a valid push subscription")
        rec = {"id": self._sid(endpoint), "user_id": user_id, "device": (device or "")[:80],
               "subscription": {"endpoint": endpoint, "keys": {"p256dh": str(keys["p256dh"]), "auth": str(keys["auth"])}},
               "created_at": time.time(), "last_ok": None, "failures": 0}
        prev = self._get(rec["id"])
        if prev and prev["user_id"] != user_id:  # device changed hands: re-own it
            self._del(rec["id"])
        self._put(rec)
        return {"id": rec["id"], "device": rec["device"]}

    def unsubscribe(self, user_id: str, endpoint: str) -> bool:
        rec = self._get(self._sid(endpoint or ""))
        if not rec or rec["user_id"] != user_id:
            return False
        self._del(rec["id"])
        return True

    def subscriptions(self, user_id: str) -> list[dict]:
        if self.backend.db is not None:
            return self.backend.db.kv_list("push_subs", user_id=user_id, limit=50)
        return [r for r in self._mem.values() if r["user_id"] == user_id]

    def _get(self, sid):
        return self.backend.db.kv_get("push_subs", sid) if self.backend.db is not None else self._mem.get(sid)

    def _put(self, rec):
        if self.backend.db is not None:
            self.backend.db.kv_put("push_subs", rec["id"], rec, user_id=rec["user_id"])
        else:
            self._mem[rec["id"]] = rec

    def _del(self, sid):
        if self.backend.db is not None:
            self.backend.db.kv_delete("push_subs", sid)
        else:
            self._mem.pop(sid, None)

    # -- delivery -------------------------------------------------------------------
    @staticmethod
    def payload_for(note: dict) -> dict:
        return {"title": note.get("title", "OpenMuse")[:120], "body": (note.get("body") or "")[:200],
                "note_id": note.get("id"), "kind": note.get("kind"),
                "tag": note.get("id"), "url": "/?note=" + str(note.get("id", ""))}

    def _on_note(self, user_id: str, note: dict) -> None:
        subs = self.subscriptions(user_id)
        if subs:
            threading.Thread(target=self.send, args=(user_id, self.payload_for(note), subs),
                             daemon=True, name="push").start()

    def send(self, user_id: str, payload: dict, subs: list[dict] | None = None) -> dict:
        """Deliver to every device; returns {sent, dropped, failed}."""
        out = {"sent": 0, "dropped": 0, "failed": 0}
        data = json.dumps(payload)
        for rec in (subs if subs is not None else self.subscriptions(user_id)):
            try:
                status = self._sender(rec["subscription"], data)
            except Exception:
                status = 0
            with self._lock:
                if status in (200, 201, 202):
                    rec["last_ok"], rec["failures"] = time.time(), 0
                    self._put(rec)
                    out["sent"] += 1
                elif status in (404, 410):  # gone: the device unsubscribed
                    self._del(rec["id"])
                    out["dropped"] += 1
                else:
                    rec["failures"] = rec.get("failures", 0) + 1
                    if rec["failures"] >= 10:
                        self._del(rec["id"])
                    else:
                        self._put(rec)
                    out["failed"] += 1
        return out

    def _webpush(self, subscription: dict, data: str) -> int:
        from pywebpush import WebPushException, webpush
        from py_vapid import Vapid
        vapid = Vapid.from_pem(self._private_pem.encode())
        try:
            resp = webpush(subscription_info=subscription, data=data, vapid_private_key=vapid,
                           vapid_claims={"sub": self.subject}, ttl=3600, timeout=10,
                           headers={"Urgency": "high"})
            return resp.status_code
        except WebPushException as exc:
            return exc.response.status_code if exc.response is not None else 0
