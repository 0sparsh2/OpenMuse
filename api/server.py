"""
HTTP server for the external API (stdlib only — no new dependencies).

ThreadingHTTPServer + a small router. Every request passes through:
auth (bearer API key + scope) -> rate limit -> idempotency (mutating calls)
-> handler -> request audit log. Errors are always the machine-readable
envelope from api.errors; successes are JSON except the SSE event stream.

Versioning: everything lives under /v1. The deprecation policy is published
in /openapi.json (x-versioning).
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import errors as E
from .auth import has_scope
from .backend import ApiBackend, _NotFound, _NotPending, _Expired, _HashMismatch, _Terminal
from .eventbus import format_sse
from .idempotency import KeyReuseError, fingerprint_body
from .openapi import build_openapi

API_VERSION = "v1"


def _exc_info(exc: Exception):
    if isinstance(exc, _NotFound):
        return E.NOT_FOUND
    if isinstance(exc, _NotPending):
        return E.APPROVAL_NOT_PENDING
    if isinstance(exc, _Expired):
        return E.APPROVAL_EXPIRED
    if isinstance(exc, _HashMismatch):
        return E.APPROVAL_HASH_MISMATCH
    if isinstance(exc, _Terminal):
        return E.RUN_ALREADY_TERMINAL
    if isinstance(exc, KeyError):
        return E.NOT_FOUND
    if isinstance(exc, ValueError):
        return E.VALIDATION_ERROR
    return E.INTERNAL_ERROR


class ApiRequestHandler(BaseHTTPRequestHandler):
    backend: ApiBackend = None  # set by serve()
    server_version = "OpenMuseAPI/1.0"

    # -- routing ------------------------------------------------------------
    # (method, pattern, handler, required_scope, idempotent)
    ROUTES = [
        ("GET", r"^/v1$", "version", None, False),
        ("POST", r"^/v1/sessions$", "create_session", "sessions:write", False),
        ("GET", r"^/v1/sessions/(?P<sid>[^/]+)$", "get_session", "sessions:read", False),
        ("POST", r"^/v1/sessions/(?P<sid>[^/]+)/messages$", "post_message", "runs:write", True),
        ("POST", r"^/v1/chats/(?P<sid>[^/]+)/messages$", "post_message", "runs:write", True),
        ("GET", r"^/v1/runs/(?P<rid>[^/]+)$", "get_run", "runs:read", False),
        ("GET", r"^/v1/runs/(?P<rid>[^/]+)/events$", "stream_events", "runs:read", False),
        ("POST", r"^/v1/runs/(?P<rid>[^/]+)/cancel$", "cancel_run", "runs:write", False),
        ("GET", r"^/v1/approvals/(?P<aid>[^/]+)$", "get_approval", "approvals:read", False),
        ("POST", r"^/v1/approvals/(?P<aid>[^/]+)/decision$", "decide_approval", "approvals:decide", False),
        ("POST", r"^/v1/artifacts$", "upload_artifact", "artifacts:write", True),
        ("GET", r"^/v1/artifacts/(?P<aid>[^/]+)$", "download_artifact", "artifacts:read", False),
        ("GET", r"^/v1/webhooks$", "list_webhooks", "webhooks:write", False),
        ("POST", r"^/v1/webhooks$", "create_webhook", "webhooks:write", False),
        ("DELETE", r"^/v1/webhooks/(?P<wid>[^/]+)$", "delete_webhook", "webhooks:write", False),
        ("GET", r"^/openapi.json$", "openapi_doc", None, False),
    ]

    # -- framework ----------------------------------------------------------
    def log_message(self, fmt, *args):  # quiet; audit log covers observability
        pass

    def _send_json(self, status: int, obj: dict, extra_headers: dict | None = None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", self._request_id)
        self.send_header("X-API-Version", API_VERSION)
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, spec, details: dict | None = None):
        code, status, message = spec
        self._send_json(status, E.envelope(code, message, self._request_id, details))

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def _parse_json(self):
        raw = self._read_body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}")

    def _route(self):
        path = self.path.split("?", 1)[0]
        for method, pattern, handler, scope, idempotent in self.ROUTES:
            if self.command != method:
                continue
            m = re.match(pattern, path)
            if m:
                return handler, m.groupdict(), scope, idempotent
        return None, {}, None, False

    def _dispatch(self):
        self._request_id = "req_" + uuid.uuid4().hex[:12]
        started = time.monotonic()
        backend = self.backend
        status = 500
        key_id = "-"
        try:
            handler_name, params, scope, idempotent = self._route()
            if handler_name is None:
                self._send_error(E.NOT_FOUND, {"path": self.path})
                status = 404
                return

            # -- auth -------------------------------------------------------
            if scope is not None:
                rec = backend.keys.authenticate(self.headers.get("Authorization"))
                if rec is None:
                    if not self.headers.get("Authorization"):
                        self._send_error(E.MISSING_API_KEY)
                    else:
                        self._send_error(E.INVALID_API_KEY)
                    status = 401
                    return
                if not has_scope(rec, scope):
                    self._send_error(E.INSUFFICIENT_SCOPE, {"required": scope})
                    status = 403
                    return
                key_id = rec.key_id
                allowed, retry_after = backend.rate_limiter.check(
                    rec.key_id, per_minute=rec.rate_limit_per_min)
                if not allowed:
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Retry-After", str(int(retry_after)))
                    self.send_header("X-Request-ID", self._request_id)
                    self.end_headers()
                    self.wfile.write(json.dumps(
                        E.envelope(*E.RATE_LIMITED, self._request_id)).encode())
                    status = 429
                    return
                self._key = rec

            # -- idempotency (mutating calls) --------------------------------
            idem_key = self.headers.get("Idempotency-Key", "").strip()
            raw_body = b""
            if idempotent and idem_key:
                raw_body = self._read_body()
                fp = fingerprint_body(raw_body)
                try:
                    replay = backend.idempotency.lookup(
                        key_id=key_id, method=self.command,
                        path=self.path.split("?", 1)[0],
                        idem_key=idem_key, fingerprint=fp)
                except KeyReuseError:
                    self._send_error(E.IDEMPOTENCY_KEY_REUSED, {"key": idem_key})
                    status = 422
                    return
                if replay is not None:
                    self._send_json(replay["status"], replay["body"],
                                    {"X-Idempotent-Replayed": "true"})
                    status = replay["status"]
                    return
                self._parsed_body = json.loads(raw_body.decode("utf-8") or "{}")
                self._idem = (key_id, self.command, self.path.split("?", 1)[0],
                              idem_key, fp)
            else:
                self._idem = None
                self._parsed_body = None

            handler = getattr(self, "h_" + handler_name)
            result = handler(params)
            if result is None:
                # Handler streamed its own response (SSE); audit as 200.
                status = 200
                return
            result_status, result_obj, extra = result
            status = result_status
            if self._idem is not None and result_status < 400:
                kid, method, path, ikey, fp = self._idem
                backend.idempotency.store(
                    key_id=kid, method=method, path=path, idem_key=ikey,
                    fingerprint=fp, status=result_status, body=result_obj)
            self._send_json(result_status, result_obj, extra)
        except Exception as exc:  # noqa: BLE001 - every error becomes an envelope
            spec = _exc_info(exc)
            code, st, message = spec
            if spec is E.INTERNAL_ERROR:
                message = "An unexpected server error occurred."
            self._send_error(spec, {"detail": str(exc)[:200]} if spec is E.INTERNAL_ERROR else None)
            status = st
        finally:
            backend.log_request(
                request_id=self._request_id, method=self.command,
                path=self.path.split("?", 1)[0], status=status,
                latency_ms=(time.monotonic() - started) * 1000.0, key_id=key_id)

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    # -- handlers -----------------------------------------------------------
    def h_version(self, params):
        return 200, {
            "api_version": API_VERSION,
            "supported_versions": [API_VERSION],
            "deprecation_policy": (
                "Breaking changes ship as a new /vN prefix; the previous major "
                "is supported >= 6 months after the new version's GA, announced "
                "via Deprecation/Sunset headers and the changelog."
            ),
        }, None

    def h_openapi_doc(self, params):
        return 200, build_openapi(), None

    def h_create_session(self, params):
        body = self._parse_json()
        rec = self.backend.create_session(
            user_id=body.get("user_id", "user_api"), title=body.get("title", ""))
        return 201, {"session_id": rec.session_id, "chat_id": rec.chat_id,
                     "user_id": rec.user_id}, None

    def h_get_session(self, params):
        rec = self.backend.sessions.get(params["sid"])
        if rec is None or rec.tenant_id != self.backend.tenant_id:
            raise _NotFound("session")
        return 200, {"session_id": rec.session_id, "chat_id": rec.chat_id,
                     "user_id": rec.user_id, "title": rec.title,
                     "created_at": rec.created_at}, None

    def h_post_message(self, params):
        body = self._parsed_body if self._parsed_body is not None else self._parse_json()
        sid = params["sid"]
        if sid not in self.backend.sessions:
            raise _NotFound("session/chat")
        idem_key = self.headers.get("Idempotency-Key", "").strip()
        run, created, msg_id = self.backend.submit_message(
            chat_id=sid, user_id=body.get("user_id", "user_api"),
            content=body.get("content", []), idempotency_key=idem_key)
        return (201 if created else 200), {
            "message_id": msg_id if created else "msg_replayed",
            "run_id": run.run_id,
            "status": run.state,
            "stream_url": f"/v1/runs/{run.run_id}/events",
        }, None

    def h_get_run(self, params):
        run = self._get_run(params["rid"])
        return 200, {
            "run_id": run.run_id, "chat_id": run.chat_id, "state": run.state,
            "final_text": run.final_text or None,
            "failure_code": run.failure_code or None,
            "failure_message": run.failure_message or None,
            "model_calls_used": run.model_calls_used,
            "tool_calls_used": run.tool_calls_used,
        }, None

    def _get_run(self, rid: str):
        try:
            run = self.backend.runs.get(rid)
        except KeyError:
            raise _NotFound("run")
        if run.tenant_id != self.backend.tenant_id:
            raise _NotFound("run")
        return run

    def h_stream_events(self, params):
        from urllib.parse import urlparse, parse_qs
        run = self._get_run(params["rid"])
        bus = self.backend.eventbus
        last = self.headers.get("Last-Event-ID")
        if last is None:
            qs = parse_qs(urlparse(self.path).query)
            last = (qs.get("last_event_id") or [None])[0]
        try:
            last_id = int(last) if last not in (None, "") else -1
        except ValueError:
            raise ValueError("Last-Event-ID must be an integer sequence number")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Request-ID", self._request_id)
        self.send_header("X-API-Version", API_VERSION)
        self.end_headers()

        from agent.models import TERMINAL as _TERMINAL
        deadline = time.monotonic() + 20.0
        sent = 0
        try:
            while True:
                for evt in bus.read_since(run.run_id, last_id):
                    self.wfile.write(format_sse(evt))
                    last_id = evt.seq
                    sent += 1
                self.wfile.flush()
                if run.state in _TERMINAL:
                    break
                if time.monotonic() > deadline:
                    break
                time.sleep(0.2)
                # refresh run state without holding stale refs
                run = self._get_run(params["rid"])
        except (BrokenPipeError, ConnectionResetError):
            pass
        return None  # response already streamed; skip _send_json

    def h_cancel_run(self, params):
        result = self.backend.cancel_run(params["rid"])
        return 200, result, None

    def h_get_approval(self, params):
        req = self.backend.approvals.requests.get(params["aid"])
        if req is None or req.tenant_id != self.backend.tenant_id:
            raise _NotFound("approval")
        return 200, {
            "approval_id": req.id, "run_id": req.run_id, "tool": req.tool_name,
            "tool_version": req.tool_version, "risk": req.risk,
            "argument_hash": req.argument_hash, "presentation": req.bind_fields,
            "status": req.status, "created_at": req.created_at,
            "expires_in_seconds": req.expires_in_seconds,
        }, None

    def h_decide_approval(self, params):
        body = self._parse_json()
        decision = body.get("decision")
        if decision not in ("approve", "deny"):
            raise ValueError("decision must be 'approve' or 'deny'")
        if not body.get("argument_hash"):
            raise ValueError("argument_hash is required")
        result = self.backend.decide_approval(
            params["aid"], decision=decision,
            argument_hash=body["argument_hash"],
            decided_by=f"api:{self._key.key_id}")
        return 200, result, None

    def h_upload_artifact(self, params):
        body = self._parsed_body if self._parsed_body is not None else self._parse_json()
        if not body.get("name") or not body.get("content_base64"):
            raise ValueError("name and content_base64 are required")
        try:
            rec = self.backend.store_artifact(
                name=body["name"], content_b64=body["content_base64"],
                content_type=body.get("content_type", ""))
        except Exception:
            raise ValueError("content_base64 is not valid base64")
        return 201, {"artifact_id": rec.artifact_id, "name": rec.name,
                     "size": rec.size, "sha256": rec.sha256}, None

    def h_download_artifact(self, params):
        import base64
        rec, raw = self.backend.get_artifact(params["aid"])
        return 200, {"artifact_id": rec.artifact_id, "name": rec.name,
                     "content_type": rec.content_type, "size": rec.size,
                     "sha256": rec.sha256,
                     "content_base64": base64.b64encode(raw).decode("ascii")}, None

    def h_list_webhooks(self, params):
        subs = self.backend.webhooks.list(self.backend.tenant_id)
        return 200, {"webhooks": [
            {"id": s.id, "url": s.url, "events": s.events, "active": s.active}
            for s in subs]}, None

    def h_create_webhook(self, params):
        body = self._parse_json()
        url = body.get("url", "")
        events = body.get("events", [])
        if not url.startswith(("https://", "http://localhost", "http://127.")):
            raise ValueError("url must be https (localhost allowed for testing)")
        allowed = {"run.completed", "run.failed"}
        if not events or not set(events) <= allowed:
            raise ValueError(f"events must be a non-empty subset of {sorted(allowed)}")
        sub = self.backend.webhooks.subscribe(
            tenant_id=self.backend.tenant_id, url=url, events=events)
        return 201, {"id": sub.id, "url": sub.url, "events": sub.events}, None

    def h_delete_webhook(self, params):
        if not self.backend.webhooks.unsubscribe(self.backend.tenant_id, params["wid"]):
            raise _NotFound("webhook")
        return 200, {"deleted": params["wid"]}, None


def serve(backend: ApiBackend, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """Start the API server in a daemon thread; returns the server object."""
    handler = type("BoundHandler", (ApiRequestHandler,), {"backend": backend})
    server = ThreadingHTTPServer((host, port), handler)
    t = threading.Thread(target=server.serve_forever, daemon=True,
                         name="openmuse-api")
    t.start()
    return server
