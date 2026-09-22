"""Messaging channel adapters and approval deep links — Phase 8.

Turns can run over messaging surfaces (WhatsApp/Telegram-style) through a
generic ChannelAdapter interface; the mock adapter here is the deterministic
offline substitute. Approval deep links are consistent across surfaces: one
signed URL opens the right pending approval whether the user taps it in the
web client, the mobile app, or a messaging thread.

Mirrors the blueprint's Phase 8 "Mobile apps, messaging adapters, and
consistent approval deep links".
"""
from __future__ import annotations

import abc
import base64
import hashlib
import hmac
import json
import time

from ._store import utcnow
from .models import ApprovalLink, WorkItem, new_id


class BadLink(Exception):
    pass


class ChannelAdapter(abc.ABC):
    """A messaging surface the agent can converse over."""

    name: str = "channel"

    @abc.abstractmethod
    def send(self, recipient: str, text: str, *,
             deep_link: str = "") -> str:
        """Deliver a message; return the provider message id."""

    @abc.abstractmethod
    def inbound_to_work(self, inbound_id: str) -> WorkItem:
        """Translate a received message into a queue work item."""


class MockMessagingAdapter(ChannelAdapter):
    """Deterministic WhatsApp/Telegram-style adapter for demos and tests."""

    name = "mock-messaging"

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id
        self.inbound: dict[str, dict] = {}
        self.outbound: list[dict] = []

    def simulate_inbound(self, sender: str, text: str) -> str:
        inbound_id = new_id("in")
        self.inbound[inbound_id] = {
            "inbound_id": inbound_id, "sender": sender, "text": text,
            "channel": self.name, "at": utcnow(),
        }
        return inbound_id

    def inbound_to_work(self, inbound_id: str) -> WorkItem:
        msg = self.inbound[inbound_id]
        return WorkItem.new(
            run_id=new_id("run"), tenant_id=self.tenant_id, kind="channel",
            payload={"channel": self.name, "sender": msg["sender"],
                     "text": msg["text"], "inbound_id": inbound_id})

    def send(self, recipient: str, text: str, *,
             deep_link: str = "") -> str:
        mid = new_id("out")
        self.outbound.append({
            "message_id": mid, "recipient": recipient, "text": text,
            "deep_link": deep_link, "channel": self.name, "at": utcnow(),
        })
        return mid


class DeepLinkService:
    """Signed approval deep links, consistent across every surface.

    Token format: base64url(payload).base64url(hmac_sha256(secret, payload)).
    Verification is constant-time; tokens carry tenant, approval request,
    run, and expiry so any surface can resolve the right pending approval.
    """

    def __init__(self, secret: bytes, *, base_url: str = "https://app.openmuse.local"):
        if not secret:
            raise ValueError("deep-link signing secret is required")
        self._secret = secret
        self.base_url = base_url.rstrip("/")

    def issue(self, *, tenant_id: str, approval_request_id: str,
              run_id: str, ttl_s: int = 86400) -> ApprovalLink:
        now = int(time.time())
        payload = {
            "tenant_id": tenant_id,
            "approval_request_id": approval_request_id,
            "run_id": run_id,
            "iat": now,
            "exp": now + ttl_s,
        }
        raw = json.dumps(payload, sort_keys=True,
                         separators=(",", ":")).encode()
        sig = hmac.new(self._secret, raw, hashlib.sha256).digest()
        token = (base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." +
                 base64.urlsafe_b64encode(sig).decode().rstrip("="))
        issued = utcnow()
        return ApprovalLink(
            token=token, url=f"{self.base_url}/approvals/{token}",
            tenant_id=tenant_id, approval_request_id=approval_request_id,
            run_id=run_id, issued_at=issued,
            expires_at=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                     time.gmtime(now + ttl_s)))

    def resolve(self, token: str) -> dict:
        try:
            raw_b64, sig_b64 = token.split(".", 1)
        except ValueError:
            raise BadLink("malformed token")
        pad = lambda s: s + "=" * (-len(s) % 4)
        try:
            raw = base64.urlsafe_b64decode(pad(raw_b64))
            sig = base64.urlsafe_b64decode(pad(sig_b64))
        except Exception:
            raise BadLink("malformed token")
        expected = hmac.new(self._secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            raise BadLink("bad signature")
        payload = json.loads(raw.decode())
        if payload.get("exp", 0) < int(time.time()):
            raise BadLink("token expired")
        return payload
