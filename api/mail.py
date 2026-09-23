"""
Gmail extras on top of the Composio bridge (issue #8).

- gmail.save_attachment: download an email attachment into the user's
  Library (the Library's own checks apply — e.g. PDFs with scripts are
  refused). Local write, so R2.
- MailWatch: opt-in "tell me about new email". Composio's webhooks need a
  public URL, so locally we poll each opted-in user's inbox every few
  minutes and notify about new unread mail (sender + subject only). The
  first check only records what's already there, so turning it on never
  floods you. New mail can also nudge the Ideas generator (at most hourly).
"""
from __future__ import annotations

import os
import re
import threading
import time
import urllib.request
from urllib.parse import urlparse

MAX_ATTACHMENT = 25 * 1024 * 1024


def _fetch_file(f) -> tuple[bytes, str]:
    """Composio returns either a local path (SDK auto-download) or {name, s3url}."""
    if isinstance(f, str):
        path = os.path.realpath(f)
        allowed = [os.path.realpath(os.path.expanduser("~/.composio")), os.path.realpath("/tmp"),
                   os.path.realpath(os.environ.get("TMPDIR", "/tmp"))]
        if not any(path.startswith(a + os.sep) for a in allowed) or not os.path.isfile(path):
            raise ValueError("attachment file is not where the connector saves downloads")
        if os.path.getsize(path) > MAX_ATTACHMENT:
            raise ValueError("attachment is larger than 25 MB")
        with open(path, "rb") as fh:
            return fh.read(), os.path.basename(path)
    if isinstance(f, dict):
        url = str(f.get("s3url") or f.get("url") or "")
        if urlparse(url).scheme != "https":
            raise ValueError("attachment link isn't https")
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "OpenMuse"}), timeout=60) as r:
            data = r.read(MAX_ATTACHMENT + 1)
        if len(data) > MAX_ATTACHMENT:
            raise ValueError("attachment is larger than 25 MB")
        return data, str(f.get("name") or "")
    raise ValueError("no attachment in the response")


def register_attachment_tool(backend) -> None:
    from tools.registry import ToolDefinition

    def save(ctx, args):
        uid = getattr(ctx, "user_id", "") or "user_api"
        name = re.sub(r"[/\\\x00]", "_", str(args["file_name"]).strip())[:200] or "attachment"
        res = backend.apps.execute_raw(uid, "GMAIL_GET_ATTACHMENT", {
            "message_id": args["message_id"], "attachment_id": args["attachment_id"], "file_name": name})
        if not res.get("successful"):
            return res
        data = res.get("data") or {}
        try:
            blob, got_name = _fetch_file(data.get("file") if isinstance(data, dict) else None)
        except Exception as exc:
            return {"successful": False, "error": f"couldn't download the attachment: {str(exc)[:200]}"}
        try:
            rec = backend.library.add(uid, name or got_name, blob, source="gmail")
        except ValueError as exc:  # the Library refused it (unsafe file, too big, …)
            return {"successful": False, "error": str(exc)[:300]}
        return {"successful": True, "saved": {"artifact_id": rec["artifact_id"], "name": rec["name"],
                                              "size": len(blob)},
                "note": "Saved to the user's Library; use docs.read to read it."}

    backend.registry.register(ToolDefinition(
        name="gmail.save_attachment", version="1.0.0",
        description=("Save an email attachment into the user's Library so it can be read (docs.read), "
                     "filled in, or added to memory. Get message_id and attachment_id from the email's "
                     "attachment list (gmail.fetch_emails / fetch_message_by_message_id)."),
        input_schema={"type": "object", "properties": {
            "message_id": {"type": "string", "maxLength": 200},
            "attachment_id": {"type": "string", "maxLength": 2000},
            "file_name": {"type": "string", "maxLength": 200}},
            "required": ["message_id", "attachment_id", "file_name"], "additionalProperties": False},
        output_schema={"type": "object"}, capabilities=["artifacts.write"], side_effect="local_write",
        idempotency="unsafe_retry", default_timeout_ms=90_000, execute=save,
    ))


class MailWatch:
    NS = "mailwatch"

    def __init__(self, backend, *, every_minutes: int = 5, tick_seconds: int = 60, ideas_every: int = 3600):
        self.backend = backend
        self.every = every_minutes * 60
        self.tick = tick_seconds
        self.ideas_every = ideas_every
        self._mem: dict[str, dict] = {}
        self._stop = threading.Event()
        self._thread = None

    # -- settings -----------------------------------------------------------------------
    def _get(self, uid: str) -> dict:
        rec = self.backend.db.kv_get(self.NS, uid) if self.backend.db is not None else self._mem.get(uid)
        return rec or {"user_id": uid, "enabled": False, "seen": [], "last_check": 0, "last_ideas": 0,
                       "seeded": False, "last_error": ""}

    def _put(self, rec: dict) -> None:
        if self.backend.db is not None:
            self.backend.db.kv_put(self.NS, rec["user_id"], rec, user_id=rec["user_id"])
        else:
            self._mem[rec["user_id"]] = rec

    def settings(self, uid: str) -> dict:
        r = self._get(uid)
        return {"enabled": r["enabled"], "last_check": r["last_check"] or None, "last_error": r.get("last_error") or None}

    def set_enabled(self, uid: str, enabled: bool) -> dict:
        r = self._get(uid)
        r["enabled"] = bool(enabled)
        if enabled and not r.get("seeded"):
            r["last_check"] = 0  # check (and seed) on the next tick
        self._put(r)
        return self.settings(uid)

    # -- polling ---------------------------------------------------------------------------
    def check(self, uid: str) -> int:
        """Check one user's inbox now; returns how many notifications went out."""
        r = self._get(uid)
        r["last_check"] = time.time()
        if "gmail" not in (self.backend.apps.connections(uid) or {}):
            r["last_error"] = "Gmail isn't connected"
            self._put(r)
            return 0
        res = self.backend.apps.execute_raw(uid, "GMAIL_FETCH_EMAILS", {
            "query": "is:unread in:inbox newer_than:2d", "max_results": 15, "include_payload": False})
        if not res.get("successful"):
            r["last_error"] = str(res.get("error") or "check failed")[:200]
            self._put(r)
            return 0
        msgs = ((res.get("data") or {}).get("messages") or []) if isinstance(res.get("data"), dict) else []
        seen = set(r.get("seen") or [])
        new = [m for m in msgs if (m.get("messageId") or m.get("id")) and (m.get("messageId") or m.get("id")) not in seen]
        sent = 0
        if r.get("seeded"):
            for m in new[:3]:
                sender = re.sub(r"\s*<[^>]+>", "", str(m.get("sender") or "")).strip().strip('"') or "someone"
                mid = m.get("messageId") or m.get("id")
                self.backend.notify(uid, kind="email", title=f"New email from {sender[:60]}",
                                    body=str(m.get("subject") or "(no subject)")[:200],
                                    link={"tab": "chat", "email_id": mid}, dedupe_key="email:" + mid)
                sent += 1
            if len(new) > 3:
                self.backend.notify(uid, kind="email", title=f"{len(new) - 3} more new emails",
                                    body="Ask me to go through them.", link={"tab": "chat"})
            pro = getattr(self.backend, "proactive", None)
            if new and pro is not None and time.time() - r.get("last_ideas", 0) > self.ideas_every:
                r["last_ideas"] = time.time()
                threading.Thread(target=lambda: _quiet(pro.generate, uid), daemon=True).start()
        r["seen"] = ([m.get("messageId") or m.get("id") for m in msgs] + list(seen))[:300]
        r["seeded"] = True
        r["last_error"] = ""
        self._put(r)
        return sent

    def due(self) -> list[str]:
        items = (self.backend.db.kv_list(self.NS, limit=5000) if self.backend.db is not None
                 else list(self._mem.values()))
        now = time.time()
        return [r["user_id"] for r in items if r.get("enabled") and now - (r.get("last_check") or 0) >= self.every]

    def start(self) -> None:
        if self._thread is not None:
            return

        def loop():
            while not self._stop.wait(self.tick):
                for uid in self.due():
                    _quiet(self.check, uid)
        self._thread = threading.Thread(target=loop, daemon=True, name="mailwatch")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def _quiet(fn, *a):
    try:
        return fn(*a)
    except Exception as exc:  # a background check must never take the server down
        print(f"mailwatch: {exc}", flush=True)
