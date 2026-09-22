"""OpenMuseClient: the client API layer over the External API contracts.

- POST /v1/chats/{chat_id}/messages with Idempotency-Key
- SSE consumption (run.status, assistant.delta, approval.required,
  run.completed) with Last-Event-ID reconnect
- approval decisions bound to the exact argument hash
- run cancellation
- artifact upload/download through client paths
- degraded mode: queued sends when the backend is unreachable, flushed
  once connectivity returns; read-only cached views via ClientState
"""
from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass, field

from .http import HttpError, api_request
from .sse import SSEClient, SSEEvent
from .state import ClientState, assert_no_secrets
from .approvals import ApprovalCard, DeviceAuth, requires_device_auth


@dataclass
class SendResult:
    message_id: str
    run_id: str
    state: str
    stream_url: str
    replayed: bool = False


@dataclass
class QueuedSend:
    chat_id: str
    text: str
    idempotency_key: str
    queued_at_order: int = 0


class OpenMuseClient:
    def __init__(self, base: str, api_key: str, *,
                 state: ClientState | None = None):
        self.base = base.rstrip("/")
        self.key = api_key
        self.state = state or ClientState()
        self.sse = SSEClient(self.base, self.key)
        self._online = True
        self._used_keys: dict[str, str] = {}  # idempotency key -> run_id

    # -- connectivity -------------------------------------------------------
    @property
    def online(self) -> bool:
        return self._online

    def _request(self, method: str, path: str, **kw):
        try:
            return api_request(self.base, method, path, key=self.key, **kw)
        except (OSError, HttpError) as exc:
            if isinstance(exc, OSError):
                self._online = False
            raise

    def ping(self) -> bool:
        try:
            st, ver, _ = api_request(self.base, "GET", "/v1", key=self.key,
                                    timeout=5.0)
            self._online = st == 200
        except OSError:
            self._online = False
        return self._online

    # -- sessions / chats ---------------------------------------------------
    def create_session(self, title: str = "") -> dict:
        st, body, _ = self._request("POST", "/v1/sessions",
                                   body={"title": title})
        assert_no_secrets(body, label="session response")
        return body

    def get_session(self, session_id: str) -> dict:
        st, body, _ = self._request("GET", f"/v1/sessions/{session_id}")
        return body

    # -- messages -----------------------------------------------------------
    @staticmethod
    def new_idempotency_key() -> str:
        return "idem_" + uuid.uuid4().hex

    def send_message(self, chat_id: str, text: str, *,
                     idempotency_key: str | None = None) -> SendResult:
        """Send a chat message; idempotent. Queues when offline (degraded)."""
        key = idempotency_key or self.new_idempotency_key()
        assert_no_secrets(text, label="outgoing message")
        if not self._online:
            self.state.queued.append(QueuedSend(
                chat_id=chat_id, text=text, idempotency_key=key,
                queued_at_order=len(self.state.queued)).__dict__)
            self.state.log(f"queued send to {chat_id} (offline)")
            return SendResult(message_id="queued", run_id="", state="QUEUED",
                              stream_url="", replayed=False)
        st, body, headers = self._request(
            "POST", f"/v1/chats/{chat_id}/messages",
            body={"content": [{"type": "text", "text": text}]},
            headers={"Idempotency-Key": key})
        self.state.log(f"sent message to {chat_id} -> run {body.get('run_id')}")
        # The server replays the stored original response for a reused key
        # (same status, same message_id/run_id): detect via key reuse.
        replayed = key in self._used_keys \
            and self._used_keys[key] == body.get("run_id")
        self._used_keys[key] = body.get("run_id", "")
        return SendResult(
            message_id=body.get("message_id", ""),
            run_id=body.get("run_id", ""),
            state=body.get("status", ""),
            stream_url=body.get("stream_url", ""),
            replayed=replayed)

    def flush_queue(self) -> list[SendResult]:
        """Flush queued sends in order once connectivity returns."""
        results: list[SendResult] = []
        remaining: list[dict] = []
        for q in self.state.queued:
            try:
                results.append(self.send_message(
                    q["chat_id"], q["text"], idempotency_key=q["idempotency_key"]))
            except OSError:
                remaining.append(q)
        self.state.queued = [q for q in remaining]
        return results

    # -- runs / SSE ---------------------------------------------------------
    def get_run(self, run_id: str) -> dict:
        st, body, _ = self._request("GET", f"/v1/runs/{run_id}")
        return body

    def stream_run(self, run_id: str, *, stop_when=None,
                   timeout: float = 25.0) -> list[SSEEvent]:
        cursor = self.state.event_cursors.get(run_id)
        if cursor:
            self.sse.last_event_id = cursor
        events = self.sse.read_run(run_id, stop_when=stop_when, timeout=timeout)
        for ev in events:
            if ev.id:
                self.state.event_cursors[run_id] = ev.id
        return events

    def resume_stream(self, run_id: str, *, stop_when=None,
                      timeout: float = 25.0) -> list[SSEEvent]:
        """Reconnect with Last-Event-ID; the server replays missed events only."""
        return self.stream_run(run_id, stop_when=stop_when, timeout=timeout)

    def cancel_run(self, run_id: str) -> dict:
        st, body, _ = self._request("POST", f"/v1/runs/{run_id}/cancel")
        return body

    # -- approvals ----------------------------------------------------------
    def get_approval(self, approval_id: str) -> ApprovalCard:
        st, body, _ = self._request("GET", f"/v1/approvals/{approval_id}")
        return ApprovalCard.from_server(body)

    def decide_approval(self, card: ApprovalCard, decision: str, *,
                        device_auth: DeviceAuth | None = None) -> dict:
        """Decide an approval, bound to the card's exact argument hash.

        High-risk (R4/R5) cards require device authentication first; after
        auth the bound fields are re-displayed (card.device_authed) and
        the decision is submitted with the same hash the card showed.
        """
        if decision not in ("approve", "deny"):
            raise ValueError("decision must be 'approve' or 'deny'")
        card.ensure_authorized(device_auth)
        st, body, _ = self._request(
            "POST", f"/v1/approvals/{card.approval_id}/decision",
            body={"decision": decision, "argument_hash": card.argument_hash})
        return body

    # -- artifacts ----------------------------------------------------------
    def upload_artifact(self, name: str, data: bytes,
                        content_type: str = "application/octet-stream") -> dict:
        st, body, _ = self._request(
            "POST", "/v1/artifacts",
            body={"name": name,
                  "content_base64": base64.b64encode(data).decode("ascii"),
                  "content_type": content_type},
            headers={"Idempotency-Key": self.new_idempotency_key()})
        return body

    def download_artifact(self, artifact_id: str) -> tuple[dict, bytes]:
        st, body, _ = self._request("GET", f"/v1/artifacts/{artifact_id}")
        raw = base64.b64decode(body["content_base64"])
        meta = {k: v for k, v in body.items() if k != "content_base64"}
        assert_no_secrets(meta, label="artifact metadata")
        return meta, raw

    # -- webhooks -----------------------------------------------------------
    def list_webhooks(self) -> list[dict]:
        st, body, _ = self._request("GET", "/v1/webhooks")
        return body.get("webhooks", body if isinstance(body, list) else [])

    # -- cached read-only views (degraded mode) -----------------------------
    def cached_or_fetch(self, view_id: str, fetch) -> dict:
        """Return fresh cached view when offline; otherwise fetch + cache."""
        if not self._online:
            cached = self.state.get_cached_view(view_id)
            if cached is not None:
                return {"_degraded": True, **cached}
            raise ConnectionError("backend unreachable and no cached view")
        payload = fetch()
        self.state.cache_view(view_id, payload)
        return payload
