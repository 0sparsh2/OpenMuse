"""Event-hook ingress — Phase 5.

A hook subscription maps verified provider events to a run template.
Ingress verifies, in order:

1. The provider event type matches a registered, enabled hook.
2. Timestamp is inside the replay window (default ±5 minutes of now).
3. HMAC-SHA256 signature over the canonical payload verifies against the
   hook's signing secret (vault ref in production; a test secret in the demo).
4. The provider event ID was never seen before (dedup store).
5. The payload matches the hook's filter (e.g. from_domain).

The payload is UNTRUSTED data: the run template carries only the event
reference, and the run fetches canonical data through the connector rather
than trusting a large webhook body. Hook runs execute under the hook's
capability ceiling, exactly like schedule runs.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import Hook, JobInstance, new_id
from .store import ScheduleStore

REPLAY_WINDOW = timedelta(minutes=5)


class HookError(Exception):
    pass


def canonical_payload(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sign_event(secret: str, event_id: str, timestamp: str, payload: dict) -> str:
    msg = f"{event_id}.{timestamp}.".encode("utf-8") + canonical_payload(payload)
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


@dataclass
class IngressResult:
    accepted: bool
    reason: str
    instance: JobInstance | None = None


class HookIngress:
    def __init__(self, store: ScheduleStore):
        self.store = store

    def receive(self, *, provider: str, event_type: str, event_id: str,
                timestamp: str, payload: dict, signature: str,
                now: datetime | None = None) -> IngressResult:
        now = now or datetime.now(timezone.utc)

        hooks = self.store.hooks_for(provider, event_type)
        if not hooks:
            return IngressResult(False, "NO_MATCHING_HOOK")

        # 1. Replay window on the event timestamp.
        try:
            ts = datetime.fromisoformat(timestamp)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return IngressResult(False, "BAD_TIMESTAMP")
        if abs((now - ts).total_seconds()) > REPLAY_WINDOW.total_seconds():
            return IngressResult(False, "REPLAY_WINDOW_EXCEEDED")

        # 2/3. Signature + dedup, per matching hook (a secret is per hook).
        hook = None
        for candidate in hooks:
            if not self._matches_filter(candidate.filter, payload):
                continue
            expected = sign_event(candidate.signing_secret_ref, event_id,
                                  timestamp, payload)
            if hmac.compare_digest(expected, signature):
                hook = candidate
                break
        if hook is None:
            if any(self._matches_filter(h.filter, payload) for h in hooks):
                return IngressResult(False, "BAD_SIGNATURE")
            return IngressResult(False, "FILTER_NO_MATCH")

        # 4. Event-ID dedup: a duplicate delivery creates no new run.
        if self.store.seen_event(event_id):
            return IngressResult(False, "DUPLICATE_EVENT")
        self.store.mark_event_seen(event_id)

        instance = JobInstance(
            instance_id=new_id("job"),
            trigger="hook",
            hook_id=hook.hook_id,
            scheduled_for=now.isoformat(),
            instruction_snapshot=hook.instruction,
            capability_ceiling=list(hook.capability_ceiling),
            delivery_policy=dict(hook.delivery_policy),
            approval_scope=hook.approval_scope,
            dedup_key=f"{hook.hook_id}+{event_id}",
            status="queued",
        )
        self.store.put_instance(instance)
        return IngressResult(True, "ENQUEUED", instance)

    @staticmethod
    def _matches_filter(filter_spec: dict, payload: dict) -> bool:
        for key, expected in filter_spec.items():
            actual = payload.get(key)
            if isinstance(expected, dict) and "domain" in expected:
                # {"from": {"domain": "example.com"}} matches "a@example.com"
                if not isinstance(actual, str) or not actual.endswith("@" + expected["domain"]):
                    return False
            elif actual != expected:
                return False
        return True
