"""
Webhook delivery for async run completion.

Clients subscribe a URL to run.completed / run.failed events. Deliveries are
POSTs with a JSON body and an HMAC-SHA256 signature header so the receiver
can verify authenticity. The transport is injectable (tests capture
deliveries locally); failures are recorded, never fatal to the run.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from .models import WebhookSubscription, _new

SIGNATURE_HEADER = "X-OpenMuse-Signature"
DELIVERY_TIMEOUT_S = 5


def sign_payload(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def default_deliver(url: str, body: bytes, headers: dict) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=DELIVERY_TIMEOUT_S) as resp:
            return 200 <= resp.status < 300, f"http_{resp.status}"
    except Exception as exc:  # noqa: BLE001 - delivery must never raise
        return False, f"error:{type(exc).__name__}"


@dataclass
class DeliveryRecord:
    subscription_id: str
    event: str
    url: str
    ok: bool
    detail: str
    at: float = field(default_factory=time.time)


class WebhookRegistry:
    def __init__(self, deliver: Callable = default_deliver):
        self._subs: dict[str, WebhookSubscription] = {}
        self._deliver = deliver
        self.deliveries: list[DeliveryRecord] = []  # immutable audit trail
        # secret material lives here only, keyed by opaque ref; never serialized
        self._secrets: dict[str, bytes] = {}

    def subscribe(self, *, tenant_id: str, url: str, events: list[str]) -> WebhookSubscription:
        import secrets as _secrets

        sub = WebhookSubscription(
            id=_new("wh"),
            tenant_id=tenant_id,
            url=url,
            events=list(events),
            secret_ref="whsec_" + _new("s"),
        )
        self._subs[sub.id] = sub
        self._secrets[sub.secret_ref] = _secrets.token_bytes(32)
        return sub

    def list(self, tenant_id: str) -> list[WebhookSubscription]:
        return [s for s in self._subs.values() if s.tenant_id == tenant_id]

    def unsubscribe(self, tenant_id: str, sub_id: str) -> bool:
        sub = self._subs.get(sub_id)
        if sub is None or sub.tenant_id != tenant_id:
            return False
        self._secrets.pop(sub.secret_ref, None)
        del self._subs[sub_id]
        return True

    def notify(self, *, tenant_id: str, event: str, payload: dict) -> list[DeliveryRecord]:
        """Deliver to matching subscriptions; record every attempt."""
        body = json.dumps(
            {"event": event, "occurred_at": time.time(), **payload},
            ensure_ascii=False,
        ).encode("utf-8")
        records = []
        for sub in self._subs.values():
            if not sub.active or sub.tenant_id != tenant_id or event not in sub.events:
                continue
            secret = self._secrets.get(sub.secret_ref, b"")
            headers = {
                "Content-Type": "application/json",
                SIGNATURE_HEADER: sign_payload(secret, body),
            }
            ok, detail = self._deliver(sub.url, body, headers)
            rec = DeliveryRecord(subscription_id=sub.id, event=event, url=sub.url,
                                 ok=ok, detail=detail)
            self.deliveries.append(rec)
            records.append(rec)
        return records
