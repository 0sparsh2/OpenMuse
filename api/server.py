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
import os
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
        ("GET", r"^/v1/browser/sessions/(?P<bsid>[^/]+)$", "browser_session", "runs:read", False),
        ("GET", r"^/v1/browser/sessions/(?P<bsid>[^/]+)/frame$", "browser_frame", "runs:read", False),
        ("POST", r"^/v1/browser/sessions/(?P<bsid>[^/]+)/input$", "browser_input", "runs:write", False),
        ("POST", r"^/v1/runs/(?P<rid>[^/]+)/pause$", "pause_run", "runs:write", False),
        ("POST", r"^/v1/runs/(?P<rid>[^/]+)/resume$", "resume_run_ep", "runs:write", False),
        ("POST", r"^/v1/runs/(?P<rid>[^/]+)/retry$", "retry_run", "runs:write", False),
        ("GET", r"^/v1/runs/(?P<rid>[^/]+)/receipt$", "run_receipt", "runs:read", False),
        ("GET", r"^/v1/activity$", "activity", "runs:read", False),
        ("GET", r"^/v1/schedules$", "list_schedules", "runs:read", False),
        ("POST", r"^/v1/schedules$", "create_schedule", "runs:write", False),
        ("PATCH", r"^/v1/schedules/(?P<sch>[^/]+)$", "update_schedule", "runs:write", False),
        ("DELETE", r"^/v1/schedules/(?P<sch>[^/]+)$", "delete_schedule", "runs:write", False),
        ("POST", r"^/v1/schedules/(?P<sch>[^/]+)/run$", "run_schedule", "runs:write", False),
        ("GET", r"^/v1/schedules/(?P<sch>[^/]+)/history$", "schedule_history", "runs:read", False),
        ("GET", r"^/v1/notifications$", "list_notifications", "sessions:read", False),
        ("GET", r"^/v1/notifications/stream$", "notifications_stream", "sessions:read", False),
        ("POST", r"^/v1/notifications/read-all$", "notifications_read_all", "sessions:read", False),
        ("POST", r"^/v1/notifications/(?P<nid>[^/]+)/read$", "notification_read", "sessions:read", False),
        ("GET", r"^/v1/logins$", "logins_list", "sessions:read", False),
        ("POST", r"^/v1/logins$", "logins_add", "sessions:write", False),
        ("DELETE", r"^/v1/logins/(?P<lid>lg_[a-f0-9]+)$", "logins_delete", "sessions:write", False),
        ("GET", r"^/v1/push/config$", "push_config", "sessions:read", False),
        ("POST", r"^/v1/push/subscribe$", "push_subscribe", "sessions:write", False),
        ("POST", r"^/v1/push/unsubscribe$", "push_unsubscribe", "sessions:write", False),
        ("POST", r"^/v1/push/test$", "push_test", "sessions:write", False),
        ("GET", r"^/v1/goals$", "goals_list", "sessions:read", False),
        ("POST", r"^/v1/goals$", "goals_create", "sessions:write", False),
        ("PATCH", r"^/v1/goals/(?P<gid>goal_[a-f0-9]+)$", "goals_update", "sessions:write", False),
        ("GET", r"^/v1/ideas$", "ideas_list", "sessions:read", False),
        ("POST", r"^/v1/ideas$", "ideas_add", "sessions:write", False),
        ("POST", r"^/v1/ideas/generate$", "ideas_generate", "sessions:write", False),
        ("POST", r"^/v1/ideas/(?P<iid>idea_[a-f0-9]+)/(?P<act>accept|dismiss|snooze)$", "ideas_act", "sessions:write", False),
        ("GET", r"^/v1/feed$", "feed_list", "sessions:read", False),
        ("POST", r"^/v1/feed/(?P<fid>feed_[a-f0-9]+)/dismiss$", "feed_dismiss", "sessions:write", False),
        ("GET", r"^/v1/library$", "library_list", "artifacts:read", False),
        ("POST", r"^/v1/library$", "library_upload", "artifacts:write", False),
        ("GET", r"^/v1/library/(?P<aid>art_[a-f0-9]+)$", "library_download", "artifacts:read", False),
        ("DELETE", r"^/v1/library/(?P<aid>art_[a-f0-9]+)$", "library_delete", "artifacts:write", False),
        ("GET", r"^/v1/library/(?P<aid>art_[a-f0-9]+)/text$", "library_text", "artifacts:read", False),
        ("POST", r"^/v1/library/(?P<aid>art_[a-f0-9]+)/memory$", "library_to_memory", "artifacts:write", False),
        ("GET", r"^/v1/monitors$", "list_monitors", "runs:read", False),
        ("POST", r"^/v1/monitors$", "create_monitor", "runs:write", False),
        ("PATCH", r"^/v1/monitors/(?P<mid>[^/]+)$", "update_monitor", "runs:write", False),
        ("DELETE", r"^/v1/monitors/(?P<mid>[^/]+)$", "delete_monitor", "runs:write", False),
        ("POST", r"^/v1/monitors/(?P<mid>[^/]+)/check$", "check_monitor", "runs:write", False),
        ("GET", r"^/v1/apps$", "list_apps", "sessions:read", False),
        ("POST", r"^/v1/apps/(?P<tk>[a-z0-9_]+)/connect$", "connect_app", "sessions:write", False),
        ("DELETE", r"^/v1/apps/(?P<tk>[a-z0-9_]+)$", "disconnect_app", "sessions:write", False),
        ("GET", r"^/v1/chats$", "list_chats", "sessions:read", False),
        ("PATCH", r"^/v1/chats/(?P<sid>[^/]+)$", "update_chat", "sessions:write", False),
        ("GET", r"^/v1/chats/(?P<sid>[^/]+)/messages$", "chat_messages", "sessions:read", False),
        ("POST", r"^/v1/auth/signup$", "auth_signup", None, False),
        ("POST", r"^/v1/auth/login$", "auth_login", None, False),
        ("POST", r"^/v1/auth/logout$", "auth_logout", "sessions:read", False),
        ("GET", r"^/v1/auth/me$", "auth_me", "sessions:read", False),
        ("GET", r"^/v1/memory$", "memory_overview", "memory:read", False),
        ("POST", r"^/v1/memory/recall$", "memory_recall", "memory:read", False),
        ("GET", r"^/v1/memory/records$", "memory_records", "memory:read", False),
        ("GET", r"^/v1/memory/journal$", "memory_journal", "memory:read", False),
        ("GET", r"^/v1/memory/people$", "memory_people", "memory:read", False),
        ("GET", r"^/v1/memory/people/(?P<pid>[^/]+)$", "memory_person", "memory:read", False),
        ("POST", r"^/v1/memory/forget$", "memory_forget", "memory:write", False),
        ("GET", r"^/v1/memory/profile$", "memory_profile", "memory:read", False),
        ("PUT", r"^/v1/memory/profile/(?P<fname>[A-Z]+\.md)$", "memory_profile_put", "memory:write", False),
        ("GET", r"^/v1/memory/documents$", "memory_documents", "memory:read", False),
        ("POST", r"^/v1/memory/documents$", "memory_document_add", "memory:write", False),
        ("DELETE", r"^/v1/memory/documents/(?P<did>[^/]+)$", "memory_document_delete", "memory:write", False),
        ("GET", r"^/v1/memory/summaries$", "memory_summaries", "memory:read", False),
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

    def do_PUT(self):
        self._dispatch()

    def do_PATCH(self):
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

    # -- identity & ownership ------------------------------------------------
    def _is_admin(self) -> bool:
        return "admin" in self._key.scopes

    def _uid(self) -> str:
        """The caller's user id. Session tokens carry it; the admin/dev key
        acts as the legacy local user."""
        return self._key.user_id or "user_api"

    def _check_owner(self, user_id: str, what: str) -> None:
        # Developer/service API keys are tenant-level credentials; per-user
        # session tokens (from sign-in) only ever see their own resources.
        if not self._key.user_id:
            return
        if user_id != self._key.user_id:
            raise _NotFound(what)  # never reveal other users' resources exist

    def h_create_session(self, params):
        body = self._parse_json()
        uid = self._key.user_id or body.get("user_id", "user_api")
        rec = self.backend.create_session(user_id=uid, title=body.get("title", ""))
        return 201, {"session_id": rec.session_id, "chat_id": rec.chat_id,
                     "user_id": rec.user_id}, None

    def h_get_session(self, params):
        rec = self.backend.sessions.get(params["sid"])
        if rec is None or rec.tenant_id != self.backend.tenant_id:
            raise _NotFound("session")
        self._check_owner(rec.user_id, "session")
        return 200, {"session_id": rec.session_id, "chat_id": rec.chat_id,
                     "user_id": rec.user_id, "title": rec.title,
                     "created_at": rec.created_at}, None

    def h_post_message(self, params):
        body = self._parsed_body if self._parsed_body is not None else self._parse_json()
        sid = params["sid"]
        if sid not in self.backend.sessions:
            raise _NotFound("session/chat")
        session = self.backend.sessions[sid]
        self._check_owner(session.user_id, "session/chat")
        idem_key = self.headers.get("Idempotency-Key", "").strip()
        run, created, msg_id = self.backend.submit_message(
            chat_id=sid, user_id=session.user_id,
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
        self._check_owner(run.user_id, "run")
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
        watchers = self.backend._watchers
        watchers[run.run_id] = watchers.get(run.run_id, 0) + 1
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
        finally:
            watchers[run.run_id] = max(0, watchers.get(run.run_id, 1) - 1)
        return None  # response already streamed; skip _send_json

    # -- live browser view ----------------------------------------------------
    def _live_browser(self, bsid: str = ""):
        op = self.backend.browser
        if op is None or not hasattr(op, "session_info"):
            return None
        if bsid:
            info = op.session_info(bsid)
            if info and info.get("user_id") and self._key.user_id \
                    and info["user_id"] != self._key.user_id:
                return None  # another user's browser: indistinguishable from missing
            rid = self.backend._browser_runs.get(bsid)
            if rid is None:
                return None if self._key.user_id else op
            try:
                self._get_run(rid)
            except _NotFound:
                return None
        return op

    def h_browser_session(self, params):
        op = self._live_browser(params["bsid"])
        info = op.session_info(params["bsid"]) if op else None
        if info is None:
            return 404, E.envelope(E.NOT_FOUND[0], E.NOT_FOUND[2], self._request_id, {"browser_session": params["bsid"]}), None
        info = dict(info, run_id=self.backend._browser_runs.get(params["bsid"]))
        return 200, info, None

    def h_browser_frame(self, params):
        op = self._live_browser(params["bsid"])
        info = op.session_info(params["bsid"]) if op else None
        if info is None:
            return 404, E.envelope(E.NOT_FOUND[0], E.NOT_FOUND[2], self._request_id, {"browser_session": params["bsid"]}), None
        frame, seq = op.frame(params["bsid"])
        if not frame:
            return 404, E.envelope(E.NOT_FOUND[0], E.NOT_FOUND[2], self._request_id, {"frame": "none yet"}), None
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Seq", str(seq))
        self.send_header("X-Request-ID", self._request_id)
        self.end_headers()
        self.wfile.write(frame)
        return None

    def h_browser_input(self, params):
        from browser.operator import BrowserError
        op = self._live_browser(params["bsid"])
        if op is None or op.session_info(params["bsid"]) is None:
            return 404, E.envelope(E.NOT_FOUND[0], E.NOT_FOUND[2], self._request_id, {"browser_session": params["bsid"]}), None
        body = self._parse_json()
        try:
            info = op.user_input(params["bsid"], body)
        except BrowserError as exc:
            return 409, {"error": {"code": exc.code, "message": str(exc),
                                   "request_id": self._request_id}}, None
        return 200, info, None

    # -- task control (issue #6) ----------------------------------------------------
    def h_pause_run(self, params):
        self._get_run(params["rid"])
        return 200, self.backend.pause_run(params["rid"]), None

    def h_resume_run_ep(self, params):
        self._get_run(params["rid"])
        return 200, self.backend.resume_paused(params["rid"]), None

    def h_retry_run(self, params):
        self._get_run(params["rid"])
        return 201, self.backend.retry_run(params["rid"]), None

    def h_run_receipt(self, params):
        self._get_run(params["rid"])
        rec = self.backend.receipt(params["rid"])
        if rec is None:
            raise _NotFound("receipt")
        return 200, rec, None

    def h_activity(self, params):
        return 200, {"runs": self.backend.activity(self._uid())}, None

    # -- schedules (issue #4) ------------------------------------------------------
    def _sched(self):
        if self.backend.schedules is None:
            raise _NotFound("schedules")
        return self.backend.schedules

    def h_list_schedules(self, params):
        us = self._sched()
        svc = us.service_for(self._uid())
        return 200, {"schedules": [us.view(svc, s) for s in svc.list_schedules()],
                     "timezone": self.backend.user_timezone(self._uid())}, None

    def h_create_schedule(self, params):
        from scheduler.service import ScheduleError
        body = self._parse_json()
        us = self._sched()
        svc = us.service_for(self._uid())
        try:
            s = svc.create_schedule(
                name=str(body.get("name", "")).strip()[:120] or "Scheduled task",
                schedule=str(body.get("schedule", "")), kind=body.get("kind", "cron"),
                timezone=body.get("timezone") or self.backend.user_timezone(self._uid()),
                instructions=str(body.get("instructions", "")).strip()[:4000],
                run_at=str(body.get("run_at", "")),
                misfire_policy=body.get("misfire_policy", "fire_once"),
                approval_scope="Created by the user in Schedules")
        except ScheduleError as exc:
            raise ValueError(str(exc))
        return 201, {"schedule": us.view(svc, s)}, None

    def _owned_schedule(self, sid):
        us = self._sched()
        svc = us.service_for(self._uid())
        s = svc.store.get_schedule(sid)
        if s is None:
            raise _NotFound("schedule")
        return us, svc, s

    def h_update_schedule(self, params):
        from scheduler.service import ScheduleError
        us, svc, s = self._owned_schedule(params["sch"])
        body = self._parse_json()
        try:
            if "enabled" in body:
                s = svc.set_enabled(s.schedule_id, bool(body["enabled"]))
            changes = {k: body[k] for k in ("name", "instruction", "timezone") if k in body}
            if "schedule" in body:
                changes["cron_expression"] = body["schedule"]
            if changes:
                s = svc.update_schedule(s.schedule_id, **changes)
        except (ScheduleError, TypeError) as exc:
            raise ValueError(str(exc))
        return 200, {"schedule": us.view(svc, s)}, None

    def h_delete_schedule(self, params):
        us, svc, s = self._owned_schedule(params["sch"])
        svc.remove_schedule(s.schedule_id)
        return 200, {"deleted": s.schedule_id}, None

    def h_run_schedule(self, params):
        us, svc, s = self._owned_schedule(params["sch"])
        run_id = us.run_now(self._uid(), s.schedule_id)
        return 202, {"run_id": run_id, "chat_id": self.backend.runs.get(run_id).chat_id}, None

    def h_schedule_history(self, params):
        us, svc, s = self._owned_schedule(params["sch"])
        inst = sorted((i.to_dict() for i in svc.store.instances_for_schedule(s.schedule_id)),
                      key=lambda i: i.get("created_at", ""), reverse=True)[:50]
        return 200, {"instances": inst}, None

    # -- notifications (issue #5) ---------------------------------------------------
    def h_list_notifications(self, params):
        from urllib.parse import parse_qs, urlparse
        unread = (parse_qs(urlparse(self.path).query).get("unread") or ["0"])[0] in ("1", "true")
        items = self.backend.notifications(self._uid(), unread_only=unread)
        return 200, {"notifications": items,
                     "unread": sum(1 for n in items if not n.get("read_at")) if not unread else len(items)}, None

    def h_notification_read(self, params):
        return 200, {"marked": self.backend.mark_read(self._uid(), params["nid"])}, None

    def h_notifications_read_all(self, params):
        return 200, {"marked": self.backend.mark_read(self._uid())}, None

    def h_notifications_stream(self, params):
        """SSE: `notification` events for this user as they happen (~25s per
        connection; clients reconnect)."""
        import queue as _q
        uid = self._uid()
        inbox: "_q.Queue" = _q.Queue()
        listener = lambda user_id, note: inbox.put(note) if user_id == uid else None
        self.backend._note_listeners.append(listener)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        deadline = time.monotonic() + 25.0
        try:
            self.wfile.write(b": connected\n\n"); self.wfile.flush()
            while time.monotonic() < deadline:
                try:
                    note = inbox.get(timeout=1.0)
                except _q.Empty:
                    continue
                self.wfile.write(("event: notification\ndata: " + json.dumps(note) + "\n\n").encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.backend._note_listeners.remove(listener)
        return None

    # -- web push (issue #17) ---------------------------------------------------------
    def _push(self):
        if self.backend.push is None:
            raise _NotFound("push")
        return self.backend.push

    def h_push_config(self, params):
        p = self.backend.push
        return 200, {"enabled": p is not None, "public_key": p.public_key if p else None,
                     "devices": len(p.subscriptions(self._uid())) if p else 0}, None

    def h_push_subscribe(self, params):
        b = self._parse_json()
        rec = self._push().subscribe(self._uid(), b.get("subscription") or {}, device=str(b.get("device", "")))
        return 201, {"subscription": rec}, None

    def h_push_unsubscribe(self, params):
        b = self._parse_json()
        return 200, {"removed": self._push().unsubscribe(self._uid(), str(b.get("endpoint", "")))}, None

    def h_push_test(self, params):
        return 200, self._push().send(self._uid(), {"title": "OpenMuse", "body": "Notifications are on for this device.",
                                                    "url": "/", "tag": "push-test"}), None

    # -- saved logins (issue #16) — passwords go in, never come out -------------------
    def _vault(self):
        if self.backend.logins is None:
            raise _NotFound("saved logins")
        return self.backend.logins

    def h_logins_list(self, params):
        return 200, {"logins": self._vault().list(self._uid())}, None

    def h_logins_add(self, params):
        b = self._parse_json()
        rec = self._vault().add(self._uid(), site=str(b.get("site", "")), username=str(b.get("username", "")),
                                password=str(b.get("password", "")), label=str(b.get("label", "")))
        return 201, {"login": rec}, None

    def h_logins_delete(self, params):
        if not self._vault().delete(self._uid(), params["lid"]):
            raise _NotFound("login")
        return 200, {"deleted": params["lid"]}, None

    # -- goals / ideas / feed (issue #12) -------------------------------------------
    def _pro(self):
        if self.backend.proactive is None:
            raise _NotFound("goals")
        return self.backend.proactive

    def h_goals_list(self, params):
        return 200, {"goals": self._pro().goals(self._uid(), include_archived=True)}, None

    def h_goals_create(self, params):
        b = self._parse_json()
        g = self._pro().create_goal(self._uid(), title=str(b.get("title", "")), description=str(b.get("description", "")),
                                    target_date=str(b.get("target_date", "")), milestones=b.get("milestones") or [])
        return 201, {"goal": g}, None

    def h_goals_update(self, params):
        b = self._parse_json()
        try:
            g = self._pro().update_goal(self._uid(), params["gid"], status=str(b.get("status", "")),
                                        add_milestone=str(b.get("add_milestone", "")),
                                        complete_milestone=str(b.get("complete_milestone", "")),
                                        note=str(b.get("note", "")))
        except KeyError:
            raise _NotFound("goal")
        return 200, {"goal": g}, None

    def h_ideas_list(self, params):
        from urllib.parse import parse_qs, urlparse
        status = (parse_qs(urlparse(self.path).query).get("status") or ["open"])[0]
        return 200, {"ideas": self._pro().ideas(self._uid(), status=status)}, None

    def h_ideas_add(self, params):
        b = self._parse_json()
        idea = self._pro().propose(self._uid(), title=str(b.get("title", "")), rationale=str(b.get("rationale", "")),
                                   action_prompt=str(b.get("action_prompt", "")), source="user")
        if idea is None:
            raise ValueError("that idea is already on your list")
        return 201, {"idea": idea}, None

    def h_ideas_generate(self, params):
        return 200, {"ideas": self._pro().generate(self._uid(), notify=False)}, None

    def h_ideas_act(self, params):
        b = self._parse_json()
        pro, uid, iid = self._pro(), self._uid(), params["iid"]
        try:
            if params["act"] == "accept":
                return 201, pro.accept(uid, iid), None
            if params["act"] == "dismiss":
                return 200, {"idea": pro.dismiss(uid, iid, str(b.get("reason", "")))}, None
            return 200, {"idea": pro.snooze(uid, iid, int(b.get("days", 1) or 1))}, None
        except KeyError:
            raise _NotFound("idea")

    def h_feed_list(self, params):
        return 200, {"items": self._pro().feed(self._uid())}, None

    def h_feed_dismiss(self, params):
        try:
            self._pro().dismiss_feed(self._uid(), params["fid"])
        except KeyError:
            raise _NotFound("feed item")
        return 200, {"dismissed": params["fid"]}, None

    # -- library (issue #14) --------------------------------------------------------
    def _lib(self):
        if self.backend.library is None:
            raise _NotFound("library")
        return self.backend.library

    def h_library_list(self, params):
        lib = self._lib()
        return 200, {"documents": [lib.public(r) for r in lib.list(self._uid())]}, None

    def h_library_upload(self, params):
        import base64
        body = self._parse_json()
        try:
            data = base64.b64decode(body.get("content_base64", ""), validate=True)
        except Exception:
            raise ValueError("content_base64 is not valid base64")
        rec = self._lib().add(self._uid(), str(body.get("name", "document"))[:200], data, source="upload")
        return 201, {"document": self._lib().public(rec)}, None

    def h_library_download(self, params):
        lib = self._lib()
        rec = lib.get(self._uid(), params["aid"])
        if rec is None:
            raise _NotFound("document")
        data = lib.data(rec)
        self.send_response(200)
        self.send_header("Content-Type", rec["mime"])
        self.send_header("Content-Length", str(len(data)))
        safe_name = re.sub(r'[^\w .()-]', "_", rec["name"])
        self.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
        self.send_header("X-Request-ID", self._request_id)
        self.end_headers()
        self.wfile.write(data)
        return None

    def h_library_text(self, params):
        lib = self._lib()
        rec = lib.get(self._uid(), params["aid"])
        if rec is None:
            raise _NotFound("document")
        return 200, {"name": rec["name"], "text": lib.text(rec, limit=60_000)}, None

    def h_library_delete(self, params):
        if not self._lib().delete(self._uid(), params["aid"]):
            raise _NotFound("document")
        return 200, {"deleted": params["aid"]}, None

    def h_library_to_memory(self, params):
        lib = self._lib()
        rec = lib.get(self._uid(), params["aid"])
        if rec is None:
            raise _NotFound("document")
        if self.backend.memory is None:
            raise _NotFound("memory service")
        doc = self.backend.memory.ingest(self._uid(), rec["name"], data=lib.data(rec), filename=rec["name"])
        return 201, {"document": doc}, None

    # -- monitors (issue #11) -------------------------------------------------------
    def _mons(self):
        if self.backend.monitors is None:
            raise _NotFound("monitors")
        return self.backend.monitors

    def h_list_monitors(self, params):
        ms = self._mons()
        return 200, {"monitors": [ms.view(m) for m in ms.list(self._uid())]}, None

    def h_create_monitor(self, params):
        ms = self._mons()
        body = self._parse_json()
        m = ms.create(self._uid(), url=str(body.get("url", "")), kind=str(body.get("kind", "")),
                      target=str(body.get("target", "")), name=str(body.get("name", "")),
                      every_minutes=int(body.get("every_minutes", 60) or 60))
        return 201, {"monitor": ms.view(ms.check(m))}, None

    def _owned_monitor(self, mid):
        ms = self._mons()
        m = ms.get(self._uid(), mid)
        if m is None:
            raise _NotFound("monitor")
        return ms, m

    def h_update_monitor(self, params):
        ms, m = self._owned_monitor(params["mid"])
        body = self._parse_json()
        if "active" in body:
            m = ms.set_active(self._uid(), m["monitor_id"], bool(body["active"]))
        return 200, {"monitor": ms.view(m)}, None

    def h_delete_monitor(self, params):
        ms, m = self._owned_monitor(params["mid"])
        ms.remove(self._uid(), m["monitor_id"])
        return 200, {"deleted": m["monitor_id"]}, None

    def h_check_monitor(self, params):
        ms, m = self._owned_monitor(params["mid"])
        return 200, {"monitor": ms.view(ms.check(m))}, None

    # -- app connectors (issue #7) ------------------------------------------------
    def h_list_apps(self, params):
        if self.backend.apps is None:
            return 200, {"configured": False, "apps": []}, None
        return 200, {"configured": True, "apps": self.backend.apps.catalog(self._uid())}, None

    def h_connect_app(self, params):
        if self.backend.apps is None:
            raise _NotFound("connectors")
        body = self._parse_json()
        origin = str(body.get("return_to", ""))
        callback = origin if origin.startswith(("http://127.0.0.1", "http://localhost", "https://")) else ""
        return 200, self.backend.apps.connect(self._uid(), params["tk"], callback), None

    def h_disconnect_app(self, params):
        if self.backend.apps is None:
            raise _NotFound("connectors")
        return 200, {"disconnected": self.backend.apps.disconnect(self._uid(), params["tk"])}, None

    # -- chat threads (issue #2) --------------------------------------------------
    def _owned_session(self, sid: str):
        rec = self.backend.sessions.get(sid)
        if rec is None or rec.tenant_id != self.backend.tenant_id:
            raise _NotFound("chat")
        self._check_owner(rec.user_id, "chat")
        return rec

    def h_list_chats(self, params):
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        archived = (qs.get("archived") or ["0"])[0] in ("1", "true")
        db = self.backend.db
        if db is not None:
            rows = db.list_chats(self._uid(), archived=archived)
            return 200, {"chats": [{
                "chat_id": r["chat_id"], "title": r["title"] or "New chat",
                "archived": bool(r["archived"]), "created_at": r["created_at"],
                "updated_at": r["updated_at"], "turns": r["turns"],
                "preview": (r["last_user_text"] or "")[:120]} for r in rows]}, None
        chats = [s for s in self.backend.sessions.values() if s.user_id == self._uid()]
        chats.sort(key=lambda s: s.created_at, reverse=True)
        return 200, {"chats": [{"chat_id": s.chat_id, "title": s.title or "New chat",
                                "archived": False, "created_at": s.created_at,
                                "updated_at": s.created_at, "turns": 0, "preview": ""}
                               for s in chats]}, None

    def h_update_chat(self, params):
        rec = self._owned_session(params["sid"])
        body = self._parse_json()
        if "title" in body:
            rec.title = str(body["title"]).strip()[:120]
        if self.backend.db is not None:
            self.backend.db.update_chat(rec.chat_id, title=rec.title if "title" in body else None,
                                        archived=bool(body["archived"]) if "archived" in body else None,
                                        touch="title" in body)
        return 200, {"chat_id": rec.chat_id, "title": rec.title,
                     "archived": bool(body.get("archived", False))}, None

    def h_chat_messages(self, params):
        rec = self._owned_session(params["sid"])
        runs = [r for r in self.backend.runs._runs.values() if r.chat_id == rec.chat_id]
        runs.sort(key=lambda r: r.created_at)
        browser_by_run = {}
        for bsid, rid in self.backend._browser_runs.items():
            browser_by_run.setdefault(rid, bsid)
        turns = [{"run_id": r.run_id, "state": r.state, "created_at": r.created_at,
                  "user_text": self.backend._run_text.get(r.run_id, ""),
                  "final_text": r.final_text or "",
                  "failure_message": r.failure_message or "",
                  "browser_session": browser_by_run.get(r.run_id)} for r in runs]
        return 200, {"chat_id": rec.chat_id, "title": rec.title, "turns": turns}, None

    # -- accounts ----------------------------------------------------------------
    def _auth_error(self, exc):
        return exc.status, E.envelope(exc.code, str(exc), self._request_id), None

    def _accounts(self):
        if self.backend.accounts is None:
            raise _NotFound("accounts")
        return self.backend.accounts

    def h_auth_signup(self, params):
        from .accounts import AuthError
        body = self._parse_json()
        try:
            user, token = self._accounts().signup(email=body.get("email", ""),
                                                  password=body.get("password", ""),
                                                  name=body.get("name", ""))
        except AuthError as exc:
            return self._auth_error(exc)
        if self.backend.memory is not None:
            tz = str(body.get("timezone", "")).strip()
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(tz)
            except Exception:
                tz = ""
            self.backend.memory.ensure_profile(user["user_id"], user["name"], timezone=tz)
        return 201, {"user": user, "token": token}, None

    def h_auth_login(self, params):
        from .accounts import AuthError
        body = self._parse_json()
        try:
            user, token = self._accounts().login(email=body.get("email", ""),
                                                 password=body.get("password", ""))
        except AuthError as exc:
            return self._auth_error(exc)
        return 200, {"user": user, "token": token}, None

    def h_auth_logout(self, params):
        if self._key.user_id:
            self._accounts().logout(self._key.key_id)
        return 200, {"signed_out": True}, None

    def h_auth_me(self, params):
        if self._key.user_id and self.backend.accounts is not None:
            user = self.backend.accounts.get(self._key.user_id)
            if user is None:
                raise _NotFound("user")
            return 200, {"user": user, "kind": "user"}, None
        return 200, {"user": {"user_id": self._uid(), "name": "Developer", "email": ""},
                     "kind": "developer_key"}, None

    # -- memory (always the caller's own store) --------------------------------------
    def _mem(self):
        if self.backend.memory is None:
            raise _NotFound("memory service")
        return self.backend.memory

    def h_memory_overview(self, params):
        svc = self._mem()
        mem = svc.memory(self._uid())
        md = ""
        if mem.curated.active_records() and os.path.exists(mem.memory_md()):
            with open(mem.memory_md(), encoding="utf-8") as fh:
                md = fh.read()
        return 200, {"stats": mem.stats(), "memory_md": md}, None

    def h_memory_recall(self, params):
        body = self._parse_json()
        hits = self._mem().memory(self._uid()).recall(
            str(body.get("query", ""))[:500], top_k=min(int(body.get("top_k", 8)), 20),
            sources=body.get("sources") or None,
            include_history=bool(body.get("include_history", False)))
        return 200, {"results": hits}, None

    def h_memory_records(self, params):
        mem = self._mem().memory(self._uid())
        recs = sorted(mem.curated.all_records(), key=lambda r: r.created_at, reverse=True)
        return 200, {"records": [r.to_dict() for r in recs
                                 if r.status in ("active", "superseded")]}, None

    def h_memory_journal(self, params):
        mem = self._mem().memory(self._uid())
        entries = mem.journal.recent_entries(200)
        return 200, {"entries": [e.to_dict() for e in reversed(entries)
                                 if e.text != "[redacted]"]}, None

    def h_memory_people(self, params):
        return 200, {"people": self._mem().memory(self._uid()).people.list_people()}, None

    def h_memory_person(self, params):
        person = self._mem().memory(self._uid()).people._people.get(params["pid"])
        if person is None:
            raise _NotFound("person")
        return 200, {"person": person}, None

    def h_memory_forget(self, params):
        from memory.service import lock_for
        body = self._parse_json()
        svc = self._mem()
        mem = svc.memory(self._uid())
        query = str(body.get("query", ""))[:500]
        mode = body.get("mode") if body.get("mode") in ("tombstone", "delete") else "tombstone"
        with lock_for(svc.memory_root(self._uid())):
            plan = mem.forgetting.plan(query, mode=mode)
            out = {"status": plan.status, "note": plan.note, "targets": plan.targets}
            if body.get("confirm") and plan.status == "ready":
                result = mem.forgetting.execute(plan)
                out.update(status=result.status, removed=result.removed, verified=result.verified)
        return 200, out, None

    def h_memory_profile(self, params):
        return 200, {"files": self._mem().read_profile(self._uid())}, None

    def h_memory_profile_put(self, params):
        body = self._parse_json()
        self._mem().write_profile(self._uid(), params["fname"], str(body.get("text", "")))
        return 200, {"saved": params["fname"]}, None

    def h_memory_documents(self, params):
        return 200, {"documents": self._mem().memory(self._uid()).documents.list()}, None

    def h_memory_document_add(self, params):
        import base64
        body = self._parse_json()
        data = b""
        if body.get("content_base64"):
            data = base64.b64decode(body["content_base64"], validate=True)
            if len(data) > 15 * 1024 * 1024:
                raise ValueError("document too large (15 MB max)")
        doc = self._mem().ingest(self._uid(), str(body.get("title", ""))[:200],
                                 text=str(body.get("text", "")), data=data,
                                 filename=str(body.get("filename", ""))[:200])
        return 201, {"document": doc}, None

    def h_memory_document_delete(self, params):
        from memory.service import lock_for
        svc = self._mem()
        with lock_for(svc.memory_root(self._uid())):
            ok = svc.memory(self._uid()).documents.delete(params["did"])
        if not ok:
            raise _NotFound("document")
        return 200, {"deleted": params["did"]}, None

    def h_memory_summaries(self, params):
        return 200, {"summaries": self._mem().memory(self._uid()).summaries.list()}, None

    def h_cancel_run(self, params):
        self._get_run(params["rid"])  # ownership
        result = self.backend.cancel_run(params["rid"])
        return 200, result, None

    def _approval_for_caller(self, aid: str):
        req = self.backend.approvals.requests.get(aid)
        if req is None or req.tenant_id != self.backend.tenant_id:
            raise _NotFound("approval")
        self._get_run(req.run_id)  # ownership check via the parked run
        return req

    def h_get_approval(self, params):
        req = self._approval_for_caller(params["aid"])
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
        self._approval_for_caller(params["aid"])
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
