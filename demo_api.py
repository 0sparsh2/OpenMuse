#!/usr/bin/env python3
"""
Phase 7 proof: the external API, end to end, against a live local server.

Boots ApiBackend + HTTP server on 127.0.0.1 with a scripted mock provider,
then proves over real HTTP:
  1. version + openapi.json served
  2. auth: missing key / bad key / insufficient scope rejected
  3. create session -> send message (Idempotency-Key) -> SSE stream shows
     approval.required (the scripted model proposes files.write, R2 -> ASK)
  4. reconnect with Last-Event-ID replays only missed events, no duplicates
  5. retry with the same Idempotency-Key replays the original response;
     same key + different body -> 422 IDEMPOTENCY_KEY_REUSED
  6. approval decision: wrong argument_hash -> 409; correct hash -> approved,
     the parked run resumes and completes
  7. artifacts upload/download round-trip
  8. webhook subscription receives a signed run.completed delivery
  9. rate limit trips with 429 + Retry-After
 10. cancel on a parked run -> CANCELLED
 11. per-request audit log populated, X-Request-ID on responses

Run:  python3 demo_api.py
Exit code 0 = all checks passed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from api import ApiBackend, serve  # noqa: E402
from gateway import ModelResponse, ToolCall  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    CHECKS.append((name, ok, detail))


# --------------------------------------------------------------------------
# Scripted provider: first proposes files.write (R2 -> ASK -> parked), then,
# once the write result exists, answers. A "quick" user message skips tools.
# --------------------------------------------------------------------------
_call_seq = [0]


def mock_respond(request, history):
    tool_results = [m for m in request.messages if m.role == "tool"]
    result_names = {m.name for m in tool_results}
    user_text = " ".join(
        b.text for m in request.messages if m.role == "user" for b in m.blocks)
    if "quick" in user_text:
        return ModelResponse(text="quick answer", stop_reason="stop")
    if "files.write" in result_names:
        return ModelResponse(text="Wrote notes/api-demo.txt via the external API.",
                             stop_reason="stop")
    _call_seq[0] += 1
    return ModelResponse(
        text="",
        tool_calls=[ToolCall(
            id=f"call_api_{_call_seq[0]}", name="files.write",
            arguments={"path": "notes/api-demo.txt",
                       "content": "hello from the external api\n"})],
        stop_reason="tool_calls")


# --------------------------------------------------------------------------
# Webhook capture server
# --------------------------------------------------------------------------
captured: list[dict] = []


class CaptureHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        captured.append({"headers": dict(self.headers), "body": body})
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def start_capture():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --------------------------------------------------------------------------
# HTTP client helpers
# --------------------------------------------------------------------------
class HttpError(Exception):
    def __init__(self, status, body, headers):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body
        self.headers = headers


def _h(headers: dict, name: str) -> str | None:
    """Case-insensitive header lookup."""
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


def api(base, method, path, *, key=None, body=None, headers=None, timeout=30):
    data = None
    h = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    if key:
        h["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(base + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode()) if raw else {}, dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw.decode()) if raw else {}
        except ValueError:
            payload = {}
        raise HttpError(e.code, payload, dict(e.headers))


def read_sse(base, path, key, *, last_event_id=None, stop_when=None, timeout=25):
    """Read SSE until stop_when(event) is true or the stream ends."""
    import http.client
    host = base.split("://")[1]
    conn = http.client.HTTPConnection(host, timeout=timeout)
    headers = {"Authorization": f"Bearer {key}", "Accept": "text/event-stream"}
    if last_event_id is not None:
        headers["Last-Event-ID"] = str(last_event_id)
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    assert resp.status == 200, f"SSE status {resp.status}"
    events = []
    buf = b""
    cur: dict = {}

    def flush_block():
        if "event" in cur:
            data = cur.get("data", "")
            try:
                payload = json.loads(data) if data else {}
            except ValueError:
                payload = {"_raw": data}
            events.append({"id": int(cur.get("id", -1)), "event": cur["event"],
                           "data": payload})

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
        except Exception:
            break
        if not chunk:
            break
        buf += chunk
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            cur = {}
            for line in block.decode("utf-8", "replace").split("\n"):
                if line.startswith("id:"):
                    cur["id"] = line[3:].strip()
                elif line.startswith("event:"):
                    cur["event"] = line[6:].strip()
                elif line.startswith("data:"):
                    cur["data"] = cur.get("data", "") + line[5:].strip()
            flush_block()
            if stop_when and any(stop_when(e) for e in events):
                conn.close()
                return events
    conn.close()
    return events


def wait_for(base, key, run_id, want=("COMPLETED",), timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, run, _ = api(base, "GET", f"/v1/runs/{run_id}", key=key)
        if run["state"] in want:
            return run
        time.sleep(0.3)
    raise AssertionError(f"run {run_id} did not reach {want}")


def main() -> int:
    workspace = tempfile.mkdtemp(prefix="openmuse-api-ws-")
    backend = ApiBackend(workspace_root=workspace, respond=mock_respond)
    server = serve(backend)
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    hook_srv = start_capture()
    hook_port = hook_srv.server_address[1]

    # -- keys ---------------------------------------------------------------
    admin_rec, admin_key = backend.keys.create_key(
        name="demo-admin", scopes={"admin"}, tenant_id=backend.tenant_id)
    reader_rec, reader_key = backend.keys.create_key(
        name="demo-reader", scopes={"runs:read"}, tenant_id=backend.tenant_id)
    _, limited_key = backend.keys.create_key(
        name="demo-limited", scopes={"runs:read"}, tenant_id=backend.tenant_id,
        rate_limit_per_min=3)

    # 1. version + openapi ---------------------------------------------------
    st, ver, _ = api(base, "GET", "/v1")
    check("version endpoint", st == 200 and ver["api_version"] == "v1", str(ver.get("api_version")))
    st, spec, _ = api(base, "GET", "/openapi.json")
    check("openapi.json served", st == 200 and "/v1/chats/{chat_id}/messages" in spec["paths"])
    check("openapi documents deprecation policy",
          "deprecation_policy" in spec.get("x-versioning", {}))

    # 2. auth ----------------------------------------------------------------
    try:
        api(base, "GET", "/v1/sessions/x")
        check("missing key rejected", False)
    except HttpError as e:
        check("missing key rejected",
              e.status == 401 and e.body["error"]["code"] == "MISSING_API_KEY")
    try:
        api(base, "GET", "/v1/sessions/x", key="omk_bogus")
        check("bad key rejected", False)
    except HttpError as e:
        check("bad key rejected",
              e.status == 401 and e.body["error"]["code"] == "INVALID_API_KEY")
    try:
        api(base, "POST", "/v1/sessions", key=reader_key, body={})
        check("insufficient scope rejected", False)
    except HttpError as e:
        check("insufficient scope rejected",
              e.status == 403 and e.body["error"]["code"] == "INSUFFICIENT_SCOPE")

    # 3. session + message -> parked approval --------------------------------
    st, sess, _ = api(base, "POST", "/v1/sessions", key=admin_key,
                      body={"user_id": "u1", "title": "api demo"})
    check("create session", st == 201 and sess["session_id"], sess.get("session_id", ""))
    chat_id = sess["chat_id"]
    st, got, _ = api(base, "GET", f"/v1/sessions/{chat_id}", key=admin_key)
    check("get session", st == 200 and got["chat_id"] == chat_id)

    msg_body = {"content": [{"type": "text", "text": "write the demo file"}]}
    st, posted, hdrs = api(base, "POST", f"/v1/chats/{chat_id}/messages",
                           key=admin_key, body=msg_body,
                           headers={"Idempotency-Key": "idem-1"})
    check("send message starts run", st == 201 and posted["run_id"].startswith("run_"),
          posted.get("run_id", ""))
    check("request id header present", _h(hdrs, "X-Request-ID") is not None, _h(hdrs, "X-Request-ID") or "")
    run_id = posted["run_id"]

    events = read_sse(base, posted["stream_url"], admin_key,
                      stop_when=lambda e: e["event"] == "approval.required",
                      timeout=25)
    appr = next((e for e in events if e["event"] == "approval.required"), None)
    check("SSE stream shows approval.required", appr is not None,
          f"{len(events)} events seen")
    approval_id = appr["data"]["approval_id"] if appr else ""
    check("approval presentation bound", bool(appr and appr["data"].get("presentation")),
          str((appr or {}).get("data", {}).get("tool")))

    # 4. reconnect with Last-Event-ID ----------------------------------------
    first_seq = events[0]["id"]
    replayed = read_sse(base, posted["stream_url"], admin_key,
                        last_event_id=first_seq,
                        stop_when=lambda e: e["event"] == "approval.required",
                        timeout=25)
    seqs = [e["id"] for e in replayed]
    check("reconnect replays missed events",
          all(s > first_seq for s in seqs) and
          any(e["event"] == "approval.required" for e in replayed),
          f"seqs={seqs[:6]}")
    check("no duplicate sequence numbers", len(set(seqs)) == len(seqs))

    # 5. idempotency ----------------------------------------------------------
    st, replay, rhdrs = api(base, "POST", f"/v1/chats/{chat_id}/messages",
                            key=admin_key, body=msg_body,
                            headers={"Idempotency-Key": "idem-1"})
    check("same idempotency key replays original",
          st == 201 and replay["run_id"] == run_id and
          _h(rhdrs, "X-Idempotent-Replayed") == "true", replay.get("run_id", ""))
    try:
        api(base, "POST", f"/v1/chats/{chat_id}/messages", key=admin_key,
            body={"content": [{"type": "text", "text": "different intent"}]},
            headers={"Idempotency-Key": "idem-1"})
        check("key reuse with different body rejected", False)
    except HttpError as e:
        check("key reuse with different body rejected",
              e.status == 422 and e.body["error"]["code"] == "IDEMPOTENCY_KEY_REUSED")

    # 6. approval decision ----------------------------------------------------
    st, areq, _ = api(base, "GET", f"/v1/approvals/{approval_id}", key=admin_key)
    check("approval request visible", st == 200 and areq["status"] == "pending",
          areq.get("tool", ""))
    arg_hash = areq["argument_hash"]
    try:
        api(base, "POST", f"/v1/approvals/{approval_id}/decision", key=admin_key,
            body={"decision": "approve", "argument_hash": "sha256:tampered",
                  "client_nonce": "n1"})
        check("tampered argument_hash rejected", False)
    except HttpError as e:
        check("tampered argument_hash rejected",
              e.status == 409 and e.body["error"]["code"] == "APPROVAL_HASH_MISMATCH")
    st, decided, _ = api(base, "POST", f"/v1/approvals/{approval_id}/decision",
                         key=admin_key,
                         body={"decision": "approve", "argument_hash": arg_hash,
                               "client_nonce": "n1"})
    check("approval granted via API",
          st == 200 and decided["status"] == "approved" and decided["grant_id"],
          decided.get("grant_id", ""))
    try:
        api(base, "POST", f"/v1/approvals/{approval_id}/decision", key=admin_key,
            body={"decision": "approve", "argument_hash": arg_hash,
                  "client_nonce": "n2"})
        check("second decision on same approval rejected", False)
    except HttpError as e:
        check("second decision on same approval rejected",
              e.status == 409 and e.body["error"]["code"] == "APPROVAL_NOT_PENDING")

    run = wait_for(base, admin_key, run_id)
    check("parked run resumes and completes",
          run["state"] == "COMPLETED" and run["final_text"],
          run["state"])
    check("tool actually executed",
          os.path.exists(os.path.join(workspace, "notes", "api-demo.txt")))

    full = read_sse(base, f"/v1/runs/{run_id}/events", admin_key,
                    last_event_id=-1, timeout=10)
    types = [e["event"] for e in full]
    check("full replay ends with run.completed",
          "run.completed" in types and "assistant.delta" in types, ",".join(types))

    # 7. artifacts ------------------------------------------------------------
    payload = base64.b64encode(b"artifact-bytes").decode()
    st, art, _ = api(base, "POST", "/v1/artifacts", key=admin_key,
                     body={"name": "demo.bin", "content_base64": payload,
                           "content_type": "application/octet-stream"})
    check("artifact upload", st == 201 and art["size"] == 14, art.get("artifact_id", ""))
    st, dl, _ = api(base, "GET", f"/v1/artifacts/{art['artifact_id']}", key=admin_key)
    check("artifact download round-trips",
          st == 200 and dl["content_base64"] == payload and
          dl["sha256"] == art["sha256"])

    # 8. webhooks --------------------------------------------------------------
    st, sub, _ = api(base, "POST", "/v1/webhooks", key=admin_key,
                     body={"url": f"http://127.0.0.1:{hook_port}/hook",
                           "events": ["run.completed", "run.failed"]})
    check("webhook subscribed", st == 201 and sub["id"].startswith("wh_"))
    st, posted2, _ = api(base, "POST", f"/v1/chats/{chat_id}/messages",
                         key=admin_key, body={"content": [{"type": "text",
                         "text": "quick ping, no tools"}]})
    run2 = wait_for(base, admin_key, posted2["run_id"])
    check("quick run completes", run2["state"] == "COMPLETED")
    deadline = time.time() + 10
    while time.time() < deadline and len(captured) < 2:
        time.sleep(0.2)
    hook_hits = [c for c in captured
                 if json.loads(c["body"])["run_id"] == posted2["run_id"]]
    check("webhook delivered run.completed", len(hook_hits) == 1,
          f"{len(captured)} deliveries total")
    if hook_hits:
        hit = hook_hits[0]
        sig = _h(hit["headers"], "X-OpenMuse-Signature") or ""
        sub_rec = next(s for s in backend.webhooks.list(backend.tenant_id)
                       if s.id == sub["id"])
        secret = backend.webhooks._secrets[sub_rec.secret_ref]
        expect = "sha256=" + hmac.new(secret, hit["body"],
                                      hashlib.sha256).hexdigest()
        check("webhook signature verifies", hmac.compare_digest(sig, expect))

    # 9. rate limiting ----------------------------------------------------------
    statuses = []
    for _ in range(4):
        try:
            st, _, _ = api(base, "GET", f"/v1/runs/{run_id}", key=limited_key)
            statuses.append(st)
        except HttpError as e:
            statuses.append(e.status)
            rl_headers = e.headers
    check("rate limit trips with 429",
          statuses.count(200) == 3 and 429 in statuses, str(statuses))
    check("429 carries Retry-After",
          _h(rl_headers, "Retry-After") is not None and _h(rl_headers, "X-Request-ID"))

    # 10. cancel a parked run ----------------------------------------------------
    st, posted3, _ = api(base, "POST", f"/v1/chats/{chat_id}/messages",
                         key=admin_key, body=msg_body,
                         headers={"Idempotency-Key": "idem-cancel"})
    run3 = posted3["run_id"]
    read_sse(base, posted3["stream_url"], admin_key,
             stop_when=lambda e: e["event"] == "approval.required", timeout=25)
    st, cancelled, _ = api(base, "POST", f"/v1/runs/{run3}/cancel", key=admin_key)
    check("parked run cancelled", st == 200 and cancelled["status"] == "CANCELLED",
          cancelled.get("status", ""))
    st, got3, _ = api(base, "GET", f"/v1/runs/{run3}", key=admin_key)
    check("cancelled state visible", got3["state"] == "CANCELLED")
    try:
        api(base, "POST", f"/v1/runs/{run3}/cancel", key=admin_key)
        check("cancel on terminal run rejected", False)
    except HttpError as e:
        check("cancel on terminal run rejected",
              e.status == 409 and e.body["error"]["code"] == "RUN_ALREADY_TERMINAL")

    # 11. misc ------------------------------------------------------------------
    try:
        api(base, "GET", "/v1/runs/run_nope", key=admin_key)
        check("unknown run -> 404", False)
    except HttpError as e:
        check("unknown run -> 404",
              e.status == 404 and e.body["error"]["code"] == "NOT_FOUND")
    check("per-request audit log populated",
          len(backend.request_log) > 20 and
          all(r.request_id.startswith("req_") for r in backend.request_log),
          f"{len(backend.request_log)} requests logged")

    # 12. saying no sticks: a declined action isn't asked for again ------------------------
    seen_notice = []

    def stubborn(request, history):
        texts = " ".join(b.text for m in request.messages for b in m.blocks)
        if "USER_DECLINED" in texts or "declined shell.exec" in texts:
            seen_notice.append(True)
            if texts.count("USER_DECLINED") >= 1:
                return ModelResponse(text="Okay — I didn't run it.", stop_reason="stop")
        # a model that ignores the first notice and proposes the same command again
        return ModelResponse(text="", stop_reason="tool_calls", tool_calls=[ToolCall(
            id=f"sh{len(history or [])}", name="shell.exec", arguments={"command": "ls -la"})])
    b2 = ApiBackend(workspace_root=tempfile.mkdtemp(prefix="om-deny-"), respond=stubborn)
    chat = b2.create_session(user_id="usr_d").chat_id
    run, _, _ = b2.submit_message(chat_id=chat, user_id="usr_d", content=[{"type": "text", "text": "list my files"}])
    t0 = time.time()
    while run.state != "WAITING_FOR_APPROVAL" and time.time() - t0 < 10:
        time.sleep(0.05)
    req = b2.approvals.requests[b2._pending_approval[run.run_id]]
    b2.decide_approval(req.id, decision="deny", argument_hash=req.argument_hash, decided_by="test")
    while run.state not in ("COMPLETED", "FAILED", "CANCELLED") and time.time() - t0 < 20:
        time.sleep(0.05)
    asks = [e for e in b2.eventbus.read_since(run.run_id, -1) if e.type == "approval.required"]
    check("after Deny the model is told, and the same action isn't asked for again",
          run.state == "COMPLETED" and len(asks) == 1 and seen_notice and run.final_text.startswith("Okay"),
          f"{run.state} asks={len(asks)}")

    server.shutdown()
    hook_srv.shutdown()
    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
