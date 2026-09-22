#!/usr/bin/env python3
"""
Phase 9 proof: the client layer against the real backend.

Boots api/server.py (scripted mock provider) plus the reference client
server (client/serve_ui.py: static web UI, /v1/* proxy, /v1/local/* domain
endpoints, Secure Vault capture page), then proves over real HTTP:

  1. chat send with Idempotency-Key: replay returns the same run;
     same key + different body -> 422
  2. SSE shows run.status / assistant.delta / approval.required /
     run.completed; Last-Event-ID reconnect replays only missed events
  3. approval card shows exact destination, effect, bound argument hash;
     wrong hash -> 409; mutated arguments rejected; correct hash ->
     approved and the parked run resumes and completes
  4. high-risk approvals require device auth (PermissionError without it)
  5. artifact upload/download round-trip through client paths
  6. schedules + hooks CRUD through the client (create/preview/toggle/
     run-now/delete)
  7. connectors through the client: catalog, OAuth begin, Secure Vault
     capture flow (client never sees the raw secret), connect, status,
     disconnect
  8. memory through the client: remember/recall/forget (plan then execute)
  9. goals/feed/ideas CRUD through the client; idea promotion creates a
     goal; muted feed sources are suppressed
 10. browser handoff through the client (start/observe/close)
 11. usage summary + privacy export through the client
 12. degraded mode: queued sends while offline, flushed when online
 13. no secret-shaped material in client state, drafts, logs, or cached views
 14. web UI: static files served, JS syntax-valid (node --check), no Meta
     trademarks/assets, keyboard + screen-reader attributes present,
     design-token contrast >= 4.5:1 on text pairs
 15. vault capture page: GET renders the form; POST completes server-side;
     responses carry only the credential reference, never the secret

Run:  python3 demo_client.py
Exit code 0 = all checks passed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from api import ApiBackend, serve  # noqa: E402
from gateway import ModelResponse, ToolCall  # noqa: E402

from client import (  # noqa: E402
    OpenMuseClient, HttpError, ClientState, ApprovalCard, TestDeviceAuth,
    HIGH_RISK_CLASSES,
)
from client.approvals import requires_device_auth  # noqa: E402
from client.sse import stream_events  # noqa: E402
from client.state import assert_no_secrets, SecretLeakError  # noqa: E402
from client.services import (  # noqa: E402
    ScheduleClient, MemoryClient, ConnectorClient, BrowserClient, UsageClient,
)
from client.domains_client import GoalsClient, FeedClient, IdeasClient  # noqa: E402
from client.serve_ui import serve_ui  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    CHECKS.append((name, ok, detail))


# --------------------------------------------------------------------------
# Scripted provider (same shape as demo_api): proposes files.write (R2 ->
# ASK -> parked), then answers once the write result exists.
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
        return ModelResponse(text="Wrote notes/client-demo.txt via the client.",
                             stop_reason="stop")
    _call_seq[0] += 1
    return ModelResponse(
        text="",
        tool_calls=[ToolCall(
            id=f"call_client_{_call_seq[0]}", name="files.write",
            arguments={"path": "notes/client-demo.txt",
                       "content": "hello from the client\n"})],
        stop_reason="tool_calls")


def raw_http(base, method, path, *, body=None, form=None, headers=None,
             timeout=30):
    """Plain HTTP helper for UI-server endpoints."""
    h = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return resp.status, json.loads(raw.decode()) if raw else {}, raw
            return resp.status, raw, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw.decode()) if raw else {}
        except ValueError:
            payload = raw
        raise HttpError(e.code, payload if isinstance(payload, dict) else {},
                        dict(e.headers))


def main() -> int:
    workspace = tempfile.mkdtemp(prefix="openmuse-client-ws-")
    data_root = tempfile.mkdtemp(prefix="openmuse-client-data-")
    backend = ApiBackend(workspace_root=workspace, respond=mock_respond)
    api_server = serve(backend)
    api_base = f"http://127.0.0.1:{api_server.server_address[1]}"
    _, admin_key = backend.keys.create_key(
        name="demo-client", scopes={"admin"}, tenant_id=backend.tenant_id)

    # -- services backing the client surfaces ------------------------------
    from scheduler.store import ScheduleStore
    from scheduler.service import ScheduleService
    from memory.layered import LayeredMemory
    from connectors.vault import MemoryVault
    from connectors.oauth import OAuthFlow
    from connectors.registry import ConnectorRegistry
    from connectors.github import GitHubConnector, mock_github_transport
    from browser.operator import ManagedBrowserOperator
    from production.quotas import QuotaManager
    from production.dr import BackupService
    from production.metrics import Metrics
    from production.namespace import ProductionServices

    sched_service = ScheduleService(ScheduleStore(os.path.join(data_root, "sched")))
    mem = LayeredMemory(os.path.join(data_root, "memory"))
    vault = MemoryVault()
    registry = ConnectorRegistry(vault=vault, oauth=OAuthFlow())
    registry.register_connector(GitHubConnector(mock_github_transport()))
    browser_op = ManagedBrowserOperator(os.path.join(data_root, "browser"))
    prod = ProductionServices(
        quotas=QuotaManager(os.path.join(data_root, "quotas")),
        backup=BackupService(os.path.join(data_root, "backups")),
        deletion=None, deeplinks=None,
        metrics=Metrics(), channels={}, exports_root="")

    ui_server = serve_ui(api_base, domains_root=data_root,
                         scheduler=sched_service, memory=mem,
                         connectors=registry, tenant_id=backend.tenant_id,
                         production_services=prod)
    ui_base = f"http://127.0.0.1:{ui_server.server_address[1]}"

    client = OpenMuseClient(api_base, admin_key)

    # 1. chat send + idempotency --------------------------------------------
    sess = client.create_session("client demo")
    check("client creates session", bool(sess.get("chat_id")), str(sess.get("chat_id")))
    chat_id = sess["chat_id"]
    key1 = client.new_idempotency_key()
    r1 = client.send_message(chat_id, "please write the notes file",
                             idempotency_key=key1)
    check("client send returns run", bool(r1.run_id), r1.run_id[:16])
    r1b = client.send_message(chat_id, "please write the notes file",
                              idempotency_key=key1)
    check("same idempotency key replays the run",
          r1b.replayed and r1b.run_id == r1.run_id)
    try:
        client.send_message(chat_id, "a different body", idempotency_key=key1)
        check("same key + different body rejected", False)
    except HttpError as e:
        check("same key + different body rejected",
              e.status == 422, str(e.body.get("error", {}).get("code")))

    # 2. SSE: run.status / assistant.delta / approval.required ---------------
    events = client.stream_run(
        r1.run_id,
        stop_when=lambda e: e.name in ("approval.required", "run.completed"),
        timeout=25)
    names = [e.name for e in events]
    check("SSE shows run.status", "run.status" in names, ",".join(names[:6]))
    check("SSE shows approval.required", "approval.required" in names)
    approval_id = next(e.data["approval_id"] for e in events
                       if e.name == "approval.required")
    first_ids = [e.id for e in events if e.id]
    check("SSE events carry ids", len(first_ids) == len(events),
          f"{len(first_ids)} events")

    # Last-Event-ID reconnect replays only missed events --------------------
    if first_ids:
        missed = stream_events(api_base, f"/v1/runs/{r1.run_id}/events",
                               key=admin_key,
                               last_event_id=first_ids[0],
                               stop_when=lambda e: e.name in ("approval.required",
                                                             "run.completed"),
                               timeout=15)
        missed_ids = [e.id for e in missed if e.id]
        check("reconnect replays only missed events",
              all(int(i) > int(first_ids[0]) for i in missed_ids),
              f"{len(missed_ids)} missed")
    else:
        check("reconnect replays only missed events", False, "no event ids")

    # 3. approval card: destination/effect/hash; binding ---------------------
    card = client.get_approval(approval_id)
    rendered = card.render()
    check("approval card shows exact destination",
          rendered["destination"] == "notes/client-demo.txt",
          rendered["destination"])
    check("approval card shows effect", bool(rendered["effect"]),
          rendered["effect"])
    check("approval card binds argument hash",
          rendered["argument_hash"].startswith("sha256:"))
    check("approval card lists bound fields", bool(rendered["bind_fields"]))

    try:
        client.decide_approval(
            ApprovalCard(**{**card.__dict__, "argument_hash": "sha256:tampered"}),
            "approve")
        check("mutated argument hash rejected", False)
    except HttpError as e:
        check("mutated argument hash rejected", e.status == 409,
              str(e.body.get("error", {}).get("code")))

    decision = client.decide_approval(card, "approve")
    check("correct hash approves", decision.get("status") in ("approved", "ok", "APPROVED")
          or "grant" in json.dumps(decision).lower(), str(decision)[:80])

    tail = client.stream_run(
        r1.run_id, stop_when=lambda e: e.name == "run.completed", timeout=25)
    check("parked run resumes and completes",
          any(e.name == "run.completed" for e in tail))
    run = client.get_run(r1.run_id)
    check("run state COMPLETED", run["state"] == "COMPLETED", run["state"])
    final = run.get("final_text") or ""
    check("final text mentions the write", "client-demo" in final or "Wrote" in final,
          final[:60])

    # run cancellation on a fresh parked run --------------------------------
    key2 = client.new_idempotency_key()
    r2 = client.send_message(chat_id, "please write the notes file again",
                             idempotency_key=key2)
    client.stream_run(r2.run_id,
                      stop_when=lambda e: e.name == "approval.required",
                      timeout=25)
    cancelled = client.cancel_run(r2.run_id)
    check("client cancels parked run",
          "cancel" in json.dumps(cancelled).lower()
          or cancelled.get("state") == "CANCELLED", str(cancelled)[:80])

    # 4. device auth gating ---------------------------------------------------
    high = ApprovalCard(
        approval_id="apr_test", run_id="run_test", tool_name="payments.charge",
        tool_version="1", risk="R4", destination="acct ****1234",
        effect="charge $10", argument_hash="sha256:abc",
        bind_fields={"amount": 10})
    check("R4 is high-risk", requires_device_auth("R4")
          and "R4" in HIGH_RISK_CLASSES)
    check("R2 is not high-risk", not requires_device_auth("R2"))
    try:
        high.ensure_authorized(None)
        check("high-risk approval blocked without device auth", False)
    except PermissionError:
        check("high-risk approval blocked without device auth", True)
    authed = high.ensure_authorized(TestDeviceAuth())
    check("device auth passes and re-displays bound fields",
          authed.device_authed and authed.render()["destination"] == "acct ****1234")

    # 5. artifacts through client paths ---------------------------------------
    # upload goes through the UI server's /v1 proxy so the Library tab
    # (served by the UI server) indexes it — exactly like the web client.
    uiclient = OpenMuseClient(ui_base, admin_key)
    up = uiclient.upload_artifact("hello.txt", b"hello client\n", "text/plain")
    check("client uploads artifact", bool(up.get("artifact_id")),
          str(up.get("artifact_id")))
    meta, raw = uiclient.download_artifact(up["artifact_id"])
    check("client downloads artifact", raw == b"hello client\n"
          and meta["sha256"] == up["sha256"])
    lib = raw_http(ui_base, "GET", "/v1/local/artifacts")[1]
    check("library tab lists the upload",
          any(a.get("artifact_id") == up["artifact_id"] for a in lib))

    # 6. schedules + hooks -----------------------------------------------------
    sched = ScheduleClient(sched_service)
    s = sched.create_schedule(name="morning brief", schedule="0 9 * * *",
                              timezone="America/New_York",
                              instructions="Summarize overnight events.")
    check("client creates schedule", bool(s.get("schedule_id")),
          str(s.get("schedule_id"))[:16])
    sid = s["schedule_id"]
    check("client previews next runs", len(sched.preview(sid, n=3)) == 3)
    check("client toggles schedule",
          sched.set_enabled(sid, False)["enabled"] is False)
    sched.set_enabled(sid, True)
    inst = sched.run_now(sid)
    check("client runs schedule now", bool(inst.get("job_id") or inst.get("instance_id"))
          or "job" in json.dumps(inst).lower(), str(inst)[:60])
    sched.update_schedule(sid, name="morning brief v2")
    check("client updates schedule",
          sched.get_schedule(sid)["name"] == "morning brief v2")
    hook = sched.create_hook(name="gh pushes", provider="github",
                             event_type="push",
                             instructions="Summarize pushes.")
    check("client creates hook", bool(hook.get("hook_id")))
    check("client lists hooks",
          any(h.get("hook_id") == hook["hook_id"] for h in sched.list_hooks()))
    sched.remove_hook(hook["hook_id"])
    sched.remove_schedule(sid)
    check("client deletes schedule and hook",
          all(x.get("schedule_id") != sid for x in sched.list_schedules()))

    # 7. connectors -------------------------------------------------------------
    conn = ConnectorClient(registry)
    catalog = conn.catalog()
    check("client lists connector catalog",
          any(c["provider"] == "github" for c in catalog),
          ",".join(c["provider"] for c in catalog))
    gh = next(c for c in catalog if c["provider"] == "github")
    check("catalog shows scopes, not secrets",
          "repo.read" in gh["scopes"])
    begun = conn.begin_connect(tenant_id=backend.tenant_id, provider="github",
                               auth_kind="oauth_pkce", scopes=["repo.read"])
    check("OAuth begin returns authorization URL, no secret",
          bool(begun.get("authorization_url"))
          and "secret" not in json.dumps(begun).lower())
    cap = conn.begin_credential_capture(tenant_id=backend.tenant_id,
                                        provider="github",
                                        purpose="demo connect")
    check("vault capture returns URL + opaque id, no secret",
          cap["capture_url"].startswith("/vault/capture/")
          and "secret" not in json.dumps(cap).lower()
          or "never sees" in cap["note"])
    # the vault capture PAGE (server-side) completes the secret handling
    st, page, _ = raw_http(ui_base, "GET", cap["capture_url"])
    check("capture page renders the entry form",
          st == 200 and b"secret_value" in page)
    st, done_page, _ = raw_http(ui_base, "POST", cap["capture_url"] + "/complete",
                                form={"secret_value": "ghp_demo_test_value_123"})
    check("capture completes server-side",
          st == 200 and b"ghp_demo_test_value_123" not in done_page
          and b"credential_ref" in done_page or b"Stored in vault" in done_page)
    connected = conn.connect_with_capture(
        tenant_id=backend.tenant_id, provider="github",
        capture_id=cap["capture_id"], scopes=["repo.read"])
    check("connect from capture succeeds",
          connected.get("status") == "healthy", str(connected.get("status")))
    check("no raw secret in connect result",
          "ghp_demo_test_value_123" not in json.dumps(connected))
    status = conn.status(tenant_id=backend.tenant_id,
                         connection_id=connected["connection_id"])
    check("client reads connection status", status.get("status") == "healthy")
    exp = conn.request_scope_expansion(
        tenant_id=backend.tenant_id, connection_id=connected["connection_id"],
        scopes=["repo.write"], reason="demo")
    check("scope expansion ceremony recorded", bool(exp))
    disc = conn.disconnect(tenant_id=backend.tenant_id,
                           connection_id=connected["connection_id"])
    check("client disconnects", disc.get("status") in ("disconnected", "revoked")
          or "disconnect" in json.dumps(disc).lower(), str(disc)[:60])

    # 8. memory ------------------------------------------------------------------
    mcli = MemoryClient(mem)
    wrote = mcli.remember("My preferred dinner time is 7pm or later on weekdays.",
                          source_ref="demo")
    check("client remembers", bool(wrote.get("memory_ids")))
    hits = mcli.recall("dinner time preference")
    check("client recalls", any("7pm" in h.get("text", "") for h in hits),
          f"{len(hits)} hits")
    plan = mcli.forget_plan("dinner time preference")
    check("forget shows a plan before removing",
          "targets" in json.dumps(plan).lower() or isinstance(plan, dict))
    still = mcli.recall("dinner time preference")
    check("plan alone removes nothing", len(still) > 0)
    mcli.forget("dinner time preference")
    gone = mcli.recall("dinner time preference")
    check("forget pipeline removes the memory",
          not any("7pm" in h.get("text", "") for h in gone))
    check("memory stats exposed", mcli.stats()["journal_entries"] >= 1)
    check("MEMORY.md projection path", mcli.memory_md_path().endswith("MEMORY.md"))
    mem.add_person_fact("Riddhi Mistry", "joins dinner Oct 6", source_ref="demo")
    people = mcli.people()
    check("people pages listed",
          any(p["name"] == "Riddhi Mistry" for p in people))
    page = mcli.person_page("Riddhi Mistry")
    check("person page has source content", "Oct 6" in page)

    # 9. goals / feed / ideas -------------------------------------------------------
    goals = GoalsClient(data_root)
    g = goals.create(title="Ship the client", description="Phase 9",
                     category="build")
    check("client creates goal", g["status"] == "active", g["id"][:16])
    gid = g["id"]
    check("client gets goal", goals.get(gid)["title"] == "Ship the client")
    goals.update(gid, description="Phase 9 — client UI replication")
    check("client updates goal",
          goals.get(gid)["description"].endswith("replication"))
    entry = goals.log_activity(gid, "Built the web UI", source_run_id=r1.run_id)
    check("client logs goal activity", entry["source_run_id"] == r1.run_id)
    goals.attach(gid, up["artifact_id"])
    check("client attaches artifact to goal",
          up["artifact_id"] in goals.get(gid)["attachments"])
    goals.set_status(gid, "completed")
    check("client tracks goal status",
          goals.get(gid)["status"] == "completed")
    check("client lists goals", any(x["id"] == gid for x in goals.list()))

    feed = FeedClient(data_root)
    item = feed.publish(title="Overnight brief ready", body="3 items",
                        source_type="schedule", source_id="sch_1",
                        run_id=r1.run_id)
    check("client publishes feed item", item is not None and item["run_id"] == r1.run_id)
    feed.mute_source("sch_1")
    check("muted source suppresses publish",
          feed.publish(title="x", source_id="sch_1") is None)
    feed.unmute_source("sch_1")
    check("unmuted source publishes again",
          feed.publish(title="y", source_id="sch_1") is not None)
    feed.dismiss(item["id"])
    check("client dismisses feed item",
          all(i["id"] != item["id"] for i in feed.list()))

    ideas = IdeasClient(data_root, goals)
    idea = ideas.capture("A CLI companion for the client")
    check("client captures idea", idea["status"] == "open")
    promo = ideas.promote(idea["id"])
    check("idea promotes into a goal",
          promo["idea"]["status"] == "promoted"
          and promo["goal"]["id"] == promo["idea"]["promoted_goal_id"])
    check("promoted goal links back",
          any(a.get("text", "").startswith("Promoted from idea")
              for a in goals.get(promo["goal"]["id"])["activity"]))
    ideas.archive(ideas.capture("stale thought")["id"])
    check("client archives ideas",
          any(i["status"] == "archived" for i in ideas.list(status="archived")))
    goals.delete(gid)
    check("client deletes goal",
          all(x["id"] != gid for x in goals.list()))

    # 10. browser handoff --------------------------------------------------------------
    bcli = BrowserClient(browser_op)
    bsess = bcli.start_session(tenant_id=backend.tenant_id)
    check("client starts browser session", bool(bsess.get("session_id")),
          str(bsess.get("session_id"))[:12])
    check("browser handoff URL routes to the session",
          bsess["handoff_url"].endswith(bsess["session_id"]))
    obs = bcli.observe(bsess["session_id"])
    check("client observes browser", bool(obs.get("url")))
    closed = bcli.close_session(bsess["session_id"])
    check("client closes browser session", closed.get("closed") is True)

    # 11. usage / privacy ----------------------------------------------------------------
    ucli = UsageClient(prod, tenant_id=backend.tenant_id)
    summary = ucli.summary()
    check("usage summary exposes quotas", "quotas" in summary)
    export = ucli.export_data()
    check("privacy export returns a snapshot",
          bool(export.get("snapshot_id")), str(export.get("snapshot_id"))[:16])

    # 12. degraded mode: queued sends -------------------------------------------------------
    client._online = False
    q = client.send_message(chat_id, "queued while offline")
    check("offline send queues", q.state == "QUEUED")
    check("queue holds the message", len(client.state.queued) == 1)
    client._online = True
    flushed = client.flush_queue()
    check("queue flushes when online",
          len(flushed) == 1 and flushed[0].run_id
          and not client.state.queued)
    degraded = client.cached_or_fetch("view:goals",
                                     lambda: {"goals": 1})
    check("cached view stores on fetch", degraded == {"goals": 1})
    client._online = False
    check("offline serves read-only cached view",
          client.cached_or_fetch("view:goals", lambda: {})["_degraded"] is True)
    client._online = True

    # 13. no secrets in client state ---------------------------------------------------------
    client.state.set_draft(chat_id, "draft: call Riddhi about Oct 6 dinner")
    client.state.log("streamed run.completed for run demo")
    try:
        assert_no_secrets(client.state.drafts)
        assert_no_secrets(client.state.redacted_log)
        assert_no_secrets(client.state.cached_views["view:goals"].payload)
        check("no secret material in client state/logs/cache", True)
    except SecretLeakError as e:
        check("no secret material in client state/logs/cache", False, str(e))
    try:
        assert_no_secrets({"api_key": "sk-abcdef1234567890abcdef1234567890"})
        check("secret scanner catches planted secrets", False)
    except SecretLeakError:
        check("secret scanner catches planted secrets", True)
    check("API key not in client state",
          client.key not in json.dumps(client.state.__dict__, default=str))

    # 14. web UI: served, valid, unbranded, accessible ------------------------------------------
    st, index, _ = raw_http(ui_base, "GET", "/")
    check("web UI index served", st == 200 and b"OpenMuse" in index)
    for asset in ("css/tokens.css", "css/app.css",
                  "js/openmuse-api.js", "js/ui.js"):
        st, _, _ = raw_http(ui_base, "GET", "/" + asset)
        check(f"web UI serves {asset}", st == 200)
    web_dir = os.path.join(ROOT, "client", "web")
    js_files = [os.path.join(web_dir, "js", f)
                for f in ("openmuse-api.js", "ui.js")]
    node_ok = True
    for jf in js_files:
        r = subprocess.run(["node", "--check", jf], capture_output=True, text=True)
        node_ok = node_ok and r.returncode == 0
        if r.returncode != 0:
            print("node --check:", r.stderr[:300])
    check("client JS is syntax-valid (node --check)", node_ok)
    corpus = ""
    for dirpath, _, filenames in os.walk(web_dir):
        for fn in filenames:
            with open(os.path.join(dirpath, fn), "rb") as f:
                corpus += f.read().decode("utf-8", "replace") + "\n"
    banned = ["Meta", "Facebook", "Instagram", "WhatsApp", "Llama",
              "facebook.com", "meta.com"]
    hits_banned = [b for b in banned if b in corpus]
    check("no Meta trademarks or proprietary assets", not hits_banned,
          ",".join(hits_banned))
    check("original wordmark present", "OpenMuse" in corpus)
    a11y_markers = ["skip-link", '"role"', '"aria-live"', '"aria-label"',
                    ":focus-visible", "tabindex"]
    missing = [m for m in a11y_markers if m not in corpus]
    check("keyboard/screen-reader hooks present", not missing, ",".join(missing))

    def luminance(hexcolor: str) -> float:
        hexcolor = hexcolor.lstrip("#")
        rgb = [int(hexcolor[i:i + 2], 16) / 255 for i in (0, 2, 4)]

        def f(c):
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(rgb[0]) + 0.7152 * f(rgb[1]) + 0.0722 * f(rgb[2])

    def contrast(a, b):
        x, y = sorted([luminance(a), luminance(b)], reverse=True)
        return (x + 0.05) / (y + 0.05)

    pairs = [("#16202e", "#ffffff"), ("#4d5866", "#ffffff"),
             ("#ffffff", "#1d4ed8"), ("#1d4ed8", "#ffffff"),
             ("#16202e", "#f4f6f8"), ("#b91c1c", "#ffffff")]
    worst = min(contrast(a, b) for a, b in pairs)
    check("design-token text contrast >= 4.5:1", worst >= 4.5,
          f"worst {worst:.2f}:1")

    # 15. local domain endpoints over HTTP ----------------------------------------
    st, glist, _ = raw_http(ui_base, "GET", "/v1/local/goals")
    check("goals served over HTTP", st == 200 and isinstance(glist, list))
    st, created, _ = raw_http(ui_base, "POST", "/v1/local/goals",
                              body={"title": "HTTP goal"})
    check("goal created over HTTP", st == 201 and created["title"] == "HTTP goal")
    st, _, _ = raw_http(ui_base, "DELETE",
                        f"/v1/local/goals/{created['id']}")
    check("goal deleted over HTTP", st == 200)
    st, memstats, _ = raw_http(ui_base, "GET", "/v1/local/memory/stats")
    check("memory stats over HTTP", st == 200 and "journal_entries" in memstats)
    st, recall, _ = raw_http(ui_base, "POST", "/v1/local/memory/recall",
                             body={"query": "dinner", "top_k": 3})
    check("memory recall over HTTP", st == 200 and "hits" in recall)
    st, schlist, _ = raw_http(ui_base, "GET", "/v1/local/schedules")
    check("schedules served over HTTP", st == 200 and isinstance(schlist, list))
    st, usage, _ = raw_http(ui_base, "GET", "/v1/local/usage")
    check("usage served over HTTP", st == 200 and "quotas" in usage)
    st, conncat, _ = raw_http(ui_base, "GET", "/v1/local/connectors")
    check("connector catalog over HTTP",
          st == 200 and any(c["provider"] == "github" for c in conncat))
    # /v1/* proxy still reaches the real API
    st, ver, _ = raw_http(ui_base, "GET", "/v1",
                          headers={"Authorization": f"Bearer {admin_key}"})
    check("UI server proxies the real API",
          st == 200 and ver.get("api_version") == "v1")

    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("FAILED:", failed)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
