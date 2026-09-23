"""Phase 9 — reference client server.

Serves the static web UI (client/web/), reverse-proxies /v1/* to the real
External API (api/server.py), and exposes /v1/local/* for the client
surfaces backed by in-process services (goals/feed/ideas, memory,
schedules/hooks, connectors, usage). Also serves the Secure Vault capture
page: the secret is entered there and handled server-side — client
JavaScript never sees or stores credential values.

Run:  python3 -m client.serve_ui --api http://127.0.0.1:PORT --port 8080
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WEB_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


class UiContext:
    """Services backing /v1/local/*. Constructed once per server."""

    def __init__(self, *, domains_root: str, scheduler=None, memory=None,
                 connectors=None, tenant_id: str = "tenant_demo",
                 production_services=None, api_base: str = ""):
        self.domains_root = domains_root
        self.scheduler = scheduler
        self.memory = memory
        self.connectors = connectors
        self.tenant_id = tenant_id
        self.production_services = production_services
        self.api_base = api_base
        os.makedirs(domains_root, exist_ok=True)
        from domains import GoalStore, FeedStore, IdeaStore
        self.goals = GoalStore(os.path.join(domains_root, "domains"))
        self.feed = FeedStore(os.path.join(domains_root, "domains"))
        self.ideas = IdeaStore(os.path.join(domains_root, "domains"))
        self._artifacts_path = os.path.join(domains_root, "artifacts_index.json")
        self._lock = threading.Lock()

    # -- artifact index (library tab lists uploads made through this client)
    def record_artifact(self, meta: dict) -> None:
        with self._lock:
            items = self._read_artifacts()
            items = [a for a in items
                     if a.get("artifact_id") != meta.get("artifact_id")]
            items.append(meta)
            tmp = self._artifacts_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._artifacts_path)

    def list_artifacts(self) -> list[dict]:
        with self._lock:
            return self._read_artifacts()

    def _read_artifacts(self) -> list[dict]:
        if os.path.exists(self._artifacts_path):
            with open(self._artifacts_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return []


def _json_body(handler) -> dict:
    length = int(handler.headers.get("Content-Length", 0) or 0)
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ValueError("invalid JSON body")


class UiHandler(BaseHTTPRequestHandler):
    ctx: UiContext = None  # set by serve_ui()
    server_version = "OpenMuseUI/1.0"

    def log_message(self, fmt, *args):  # quiet; never log bodies/headers
        pass

    # -- helpers ------------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str,
              extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json")

    def _send_error(self, status: int, code: str, message: str) -> None:
        self._send_json(status, {"error": {"code": code, "message": message}})

    # -- routing ------------------------------------------------------------
    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def _dispatch(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/" or path == "/index.html":
                return self._serve_static("index.html")
            if path.startswith("/css/") or path.startswith("/js/"):
                return self._serve_static(path.lstrip("/"))
            if path.startswith("/vault/capture/"):
                return self._vault_capture(path)
            if path == "/v1" or path.startswith("/v1/") or path == "/openapi.json":
                if path.startswith("/v1/local/"):
                    return self._local(path[len("/v1/local"):])
                return self._proxy()
            return self._send_error(404, "NOT_FOUND", "unknown path")
        except ValueError as exc:
            self._send_error(400, "VALIDATION_ERROR", str(exc))
        except KeyError as exc:
            self._send_error(404, "NOT_FOUND", str(exc))
        except PermissionError as exc:
            self._send_error(403, "FORBIDDEN", str(exc))
        except Exception as exc:  # never leak internals or secrets
            self._send_error(500, "INTERNAL_ERROR", "request failed")

    # -- static ---------------------------------------------------------------
    def _serve_static(self, rel: str) -> None:
        full = os.path.normpath(os.path.join(WEB_ROOT, rel))
        if not full.startswith(WEB_ROOT) or not os.path.isfile(full):
            return self._send_error(404, "NOT_FOUND", "no such file")
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as f:
            self._send(200, f.read(), MIME.get(ext, "application/octet-stream"))

    # -- reverse proxy to the real API ----------------------------------------
    _FWD_HEADERS = ("authorization", "idempotency-key", "content-type",
                    "last-event-id", "accept")

    def _proxy(self):
        target = self.ctx.api_base + self.path
        length = int(self.headers.get("Content-Length", 0) or 0)
        data = self.rfile.read(length) if length else None
        headers = {k.title(): v for k, v in self.headers.items()
                   if k.lower() in self._FWD_HEADERS}
        req = urllib.request.Request(target, data=data, headers=headers,
                                     method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.headers.get("Content-Type", "").startswith("text/event-stream"):
                    # stream SSE through as it arrives (buffering would hide live progress)
                    self.send_response(resp.status)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    try:
                        while True:
                            chunk = resp.read1(8192)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                body = resp.read()
                self.send_response(resp.status)
                ctype = resp.headers.get("Content-Type", "application/octet-stream")
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                # record artifact uploads for the Library tab
                if self.command == "POST" and self.path == "/v1/artifacts" \
                        and resp.status in (200, 201):
                    try:
                        self.ctx.record_artifact(json.loads(body.decode("utf-8")))
                    except Exception:
                        pass
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as e:
            body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self._send_error(502, "BAD_GATEWAY", "API backend unreachable")

    # -- Secure Vault capture page (server-side; client never sees secrets) ---
    def _vault_capture(self, path: str):
        m = re.match(r"^/vault/capture/([A-Za-z0-9_\-]+)(/complete)?$", path)
        if not m or self.ctx.connectors is None:
            return self._send_error(404, "NOT_FOUND", "unknown capture")
        capture_id, complete = m.group(1), m.group(2)
        vault = self.ctx.connectors.vault
        if self.command == "GET" and not complete:
            page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Secure Vault capture — OpenMuse</title>
<link rel="stylesheet" href="/css/tokens.css"><link rel="stylesheet" href="/css/app.css">
</head><body><main class="main" style="max-width:520px">
<h1>Secure Vault capture</h1>
<p class="muted">Enter the credential for capture
<strong>{html.escape(capture_id)}</strong>. It is sent to the vault server-side;
the OpenMuse client never sees it.</p>
<form method="post" action="/vault/capture/{html.escape(capture_id)}/complete">
<label class="field"><span>Credential value</span>
<input type="password" name="secret_value" autocomplete="off" required></label>
<button class="btn" type="submit">Store in vault</button>
</form></main></body></html>"""
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if self.command == "POST" and complete:
            length = int(self.headers.get("Content-Length", 0) or 0)
            form = urllib.parse.parse_qs(
                (self.rfile.read(length) if length else b"").decode("utf-8"))
            secret = (form.get("secret_value") or [""])[0]
            if not secret:
                return self._send_error(400, "VALIDATION_ERROR", "secret required")
            try:
                ref = vault.complete_capture(capture_id, secret)
            except Exception:
                return self._send_error(400, "VALIDATION_ERROR",
                                        "capture failed or expired")
            finally:
                secret = ""  # drop the raw value immediately
            page = ("""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Stored</title>
</head><body><main class="main"><h1>Stored in vault</h1>
<p class="muted">Credential reference <span class="mono">""" +
                    html.escape(ref) + """</span> created. You can close this
window and finish connecting in OpenMuse.</p></main></body></html>""")
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        return self._send_error(405, "METHOD_NOT_ALLOWED", "bad method")

    # -- /v1/local/* ------------------------------------------------------------
    def _local(self, sub: str):
        method = self.command
        body = _json_body(self) if method in ("POST", "PUT", "PATCH") else {}
        ctx = self.ctx
        tid = ctx.tenant_id

        # goals ------------------------------------------------------------
        if sub == "/goals" and method == "GET":
            return self._send_json(200, [g.to_dict() for g in ctx.goals.list()])
        if sub == "/goals" and method == "POST":
            g = ctx.goals.create(title=body.get("title", ""),
                                 description=body.get("description", ""),
                                 category=body.get("category", ""),
                                 target_date=body.get("target_date", ""))
            return self._send_json(201, g.to_dict())
        m = re.match(r"^/goals/([^/]+)/activity$", sub)
        if m and method == "POST":
            from dataclasses import asdict
            e = ctx.goals.log_activity(m.group(1), body.get("text", ""),
                                      source=body.get("source", "manual"))
            return self._send_json(201, asdict(e))
        m = re.match(r"^/goals/([^/]+)/status$", sub)
        if m and method == "POST":
            return self._send_json(200, ctx.goals.set_status(
                m.group(1), body.get("status", "")).to_dict())
        m = re.match(r"^/goals/([^/]+)$", sub)
        if m and method == "DELETE":
            ctx.goals.delete(m.group(1))
            return self._send_json(200, {"deleted": True})

        # feed -------------------------------------------------------------
        if sub == "/feed" and method == "GET":
            return self._send_json(200, [i.to_dict() for i in ctx.feed.list()])
        if sub == "/feed" and method == "POST":
            item = ctx.feed.publish(
                title=body.get("title", ""), body=body.get("body", ""),
                source_type=body.get("source_type", "manual"),
                source_id=body.get("source_id", ""),
                run_id=body.get("run_id", ""), url=body.get("url", ""))
            return self._send_json(201, item.to_dict() if item else None)
        m = re.match(r"^/feed/([^/]+)/dismiss$", sub)
        if m and method == "POST":
            return self._send_json(200, ctx.feed.dismiss(m.group(1)).to_dict())
        if sub == "/feed/mute" and method == "POST":
            ctx.feed.mute_source(body.get("source_id", ""))
            return self._send_json(200, {"muted": True})
        if sub == "/feed/unmute" and method == "POST":
            ctx.feed.unmute_source(body.get("source_id", ""))
            return self._send_json(200, {"muted": False})

        # ideas ------------------------------------------------------------
        if sub == "/ideas" and method == "GET":
            return self._send_json(200, [i.to_dict() for i in ctx.ideas.list()])
        if sub == "/ideas" and method == "POST":
            return self._send_json(201, ctx.ideas.capture(
                body.get("text", ""), title=body.get("title", "")).to_dict())
        m = re.match(r"^/ideas/([^/]+)/promote$", sub)
        if m and method == "POST":
            idea = ctx.ideas.get(m.group(1))
            goal = ctx.goals.create(
                title=idea.title or idea.text[:60],
                description=idea.text, category="from-idea")
            ctx.goals.log_activity(
                goal.id, f"Promoted from idea {idea.id}", source="manual")
            return self._send_json(200, {
                "idea": ctx.ideas.promote(m.group(1), goal).to_dict(),
                "goal": goal.to_dict()})
        m = re.match(r"^/ideas/([^/]+)/archive$", sub)
        if m and method == "POST":
            return self._send_json(200, ctx.ideas.archive(m.group(1)).to_dict())

        # artifacts (library index) ----------------------------------------
        if sub == "/artifacts" and method == "GET":
            return self._send_json(200, ctx.list_artifacts())

        # memory -----------------------------------------------------------
        if ctx.memory is not None:
            if sub == "/memory/stats" and method == "GET":
                return self._send_json(200, ctx.memory.stats())
            if sub == "/memory/recall" and method == "POST":
                hits = ctx.memory.recall(body.get("query", ""),
                                         top_k=int(body.get("top_k", 8)))
                return self._send_json(200, {"hits": hits})
            if sub == "/memory/forget" and method == "POST":
                if body.get("confirmed"):
                    result = ctx.memory.forget(
                        body.get("query", ""),
                        mode=body.get("mode", "tombstone"))
                    from dataclasses import asdict, is_dataclass
                    result = asdict(result) if is_dataclass(result) else result
                    return self._send_json(200, {"confirmed": True,
                                                "result": result})
                plan = ctx.memory.forgetting.plan(
                    body.get("query", ""), mode=body.get("mode", "tombstone"))
                from dataclasses import asdict, is_dataclass
                plan = asdict(plan) if is_dataclass(plan) else plan
                return self._send_json(200, {"confirmed": False, "plan": plan})
            if sub == "/memory/people" and method == "GET":
                return self._send_json(200, {
                    "people": ctx.memory.people.list_people()})
            m = re.match(r"^/memory/people/(.+)$", sub)
            if m and method == "GET":
                name = urllib.parse.unquote(m.group(1))
                slug = "".join(c.lower() if c.isalnum() else "-"
                              for c in name).strip("-")
                page = ""
                ppath = os.path.join(ctx.memory.people._pages_dir, slug + ".md")
                if os.path.exists(ppath):
                    with open(ppath, "r", encoding="utf-8") as f:
                        page = f.read()
                return self._send_json(200, {"name": name, "page": page})

        # schedules & hooks -------------------------------------------------
        if ctx.scheduler is not None:
            if sub == "/schedules" and method == "GET":
                from dataclasses import asdict
                return self._send_json(200, [asdict(s) for s in
                                            ctx.scheduler.list_schedules()])
            if sub == "/schedules" and method == "POST":
                sched = ctx.scheduler.create_schedule(
                    name=body.get("name", ""), schedule=body.get("schedule", ""),
                    timezone=body.get("timezone", "UTC"),
                    instructions=body.get("instructions", ""),
                    kind=body.get("kind", "cron"))
                from dataclasses import asdict
                return self._send_json(201, asdict(sched))
            m = re.match(r"^/schedules/([^/]+)/preview$", sub)
            if m and method == "GET":
                return self._send_json(200, {
                    "preview": ctx.scheduler.preview(m.group(1), n=5)})
            m = re.match(r"^/schedules/([^/]+)/enabled$", sub)
            if m and method == "POST":
                from dataclasses import asdict
                return self._send_json(200, asdict(ctx.scheduler.set_enabled(
                    m.group(1), bool(body.get("enabled")))))
            m = re.match(r"^/schedules/([^/]+)/run$", sub)
            if m and method == "POST":
                from dataclasses import asdict
                return self._send_json(200, asdict(ctx.scheduler.run_now(m.group(1))))
            m = re.match(r"^/schedules/([^/]+)$", sub)
            if m and method == "DELETE":
                ctx.scheduler.remove_schedule(m.group(1))
                return self._send_json(200, {"deleted": True})
            if sub == "/hooks" and method == "GET":
                from dataclasses import asdict
                return self._send_json(200, [asdict(h) for h in
                                            ctx.scheduler.list_hooks()])
            if sub == "/hooks" and method == "POST":
                hook = ctx.scheduler.create_hook(
                    name=body.get("name", ""), provider=body.get("provider", ""),
                    event_type=body.get("event_type", ""),
                    instructions=body.get("instructions", ""))
                from dataclasses import asdict
                return self._send_json(201, asdict(hook))
            m = re.match(r"^/hooks/([^/]+)$", sub)
            if m and method == "DELETE":
                ctx.scheduler.remove_hook(m.group(1))
                return self._send_json(200, {"deleted": True})

        # connectors ---------------------------------------------------------
        if ctx.connectors is not None:
            if sub == "/connectors" and method == "GET":
                out = []
                for provider in sorted(
                        getattr(ctx.connectors, "_adapters", {}).keys()):
                    mm = ctx.connectors.manifest(provider)
                    out.append({
                        "provider": provider,
                        "display_name": getattr(mm, "display_name", provider),
                        "auth_kinds": list(getattr(mm, "auth_kinds", [])),
                        "scopes": list(getattr(mm, "scopes", [])),
                        "operations": [op.name for op in
                                       getattr(mm, "operations", [])]})
                return self._send_json(200, out)
            if sub == "/connectors/capture" and method == "POST":
                capture_id = ctx.connectors.vault.create_capture(
                    tenant_id=tid, provider=body.get("provider", ""),
                    purpose=body.get("purpose", "connector connect"))
                return self._send_json(200, {
                    "capture_id": capture_id,
                    "capture_url": f"/vault/capture/{capture_id}"})
            if sub == "/connectors/connect" and method == "POST":
                try:
                    result = ctx.connectors.connect(
                        tenant_id=tid, provider=body.get("provider", ""),
                        auth_kind=body.get("auth_kind", ""),
                        scopes=list(body.get("scopes", [])),
                        account_label=body.get("account_label", ""))
                except Exception as exc:
                    return self._send_error(400, "CONNECTOR_ERROR", str(exc))
                safe = {k: v for k, v in result.items()
                        if "secret" not in str(k).lower()}
                return self._send_json(200, safe)
            if sub == "/connectors/connect-capture" and method == "POST":
                try:
                    result = ctx.connectors.connect(
                        tenant_id=tid, provider=body.get("provider", ""),
                        auth_kind="api_key",
                        scopes=list(body.get("scopes", [])),
                    capture_id=body.get("capture_id", ""))
                except Exception as exc:
                    return self._send_error(400, "CONNECTOR_ERROR", str(exc))
                safe = {k: v for k, v in result.items()
                        if "secret" not in str(k).lower()}
                return self._send_json(200, safe)

        # usage / privacy ------------------------------------------------------
        if sub == "/usage" and method == "GET":
            out = {"usage": {}, "quotas": {}}
            ps = ctx.production_services
            if ps is not None:
                if hasattr(ps, "metrics"):
                    out["usage"] = ps.metrics.snapshot()
                if hasattr(ps, "quotas"):
                    out["quotas"] = ps.quotas.status(tid)
            return self._send_json(200, out)
        if sub == "/usage/export" and method == "POST":
            ps = ctx.production_services
            if ps is None:
                return self._send_error(503, "UNAVAILABLE",
                                        "no production services bound")
            snap = ps.backup.snapshot_tenant(tid, [])
            return self._send_json(200, {
                "tenant_id": tid, "snapshot_id": snap.snapshot_id,
                "stores": snap.stores, "sha256": snap.sha256})

        return self._send_error(404, "NOT_FOUND", "unknown local endpoint")


def serve_ui(api_base: str, host: str = "127.0.0.1", port: int = 0,
             **ctx_kwargs) -> ThreadingHTTPServer:
    """Start the UI server in a daemon thread; returns the server object.

    If no memory service is passed, a LayeredMemory store is created under
    <domains_root>/memory so the stock CLI wires memory automatically.
    """
    if "domains_root" not in ctx_kwargs:
        import tempfile
        ctx_kwargs["domains_root"] = tempfile.mkdtemp(prefix="openmuse-ui-")
    if ctx_kwargs.get("memory") is None:
        from memory.layered import LayeredMemory
        ctx_kwargs["memory"] = LayeredMemory(
            os.path.join(ctx_kwargs["domains_root"], "memory"))
    ctx = UiContext(api_base=api_base, **ctx_kwargs)
    handler = type("BoundUiHandler", (UiHandler,), {"ctx": ctx})
    server = ThreadingHTTPServer((host, port), handler)
    server.ui_ctx = ctx
    t = threading.Thread(target=server.serve_forever, daemon=True,
                         name="openmuse-ui")
    t.start()
    return server


def main() -> int:
    ap = argparse.ArgumentParser(description="OpenMuse reference client server")
    ap.add_argument("--api", required=True, help="API backend base URL")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--data", default="",
                    help="data root for domains (default: temp dir)")
    args = ap.parse_args()
    import tempfile
    data = args.data or tempfile.mkdtemp(prefix="openmuse-ui-")
    server = serve_ui(args.api, host=args.host, port=args.port,
                      domains_root=data)
    port = server.server_address[1]
    print(f"OpenMuse client at http://{args.host}:{port} "
          f"(API: {args.api}, data: {data})")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
