#!/usr/bin/env python3
"""Phase 8 verification: production scale, resilience, multi-surface polish.

Proves, offline and deterministically:
  1. Runs submitted to the API layer are picked up by workers off the
     durable regional queue and completed.
  2. Queue replay is idempotent — a redelivered message id never
     double-executes.
  3. A simulated regional failure re-queues in-flight work to a healthy
     region without corrupting run state (exactly-once preserved).
  4. Retryable executor failures retry with bounded attempts.
  5. Per-tenant quotas shed or defer over-budget work (backpressure) and
     account cost per provider.
  6. Provider routing falls over on transient failure, but never to a
     fallback whose data policy is incompatible with the run.
  7. Messaging adapter turns inbound messages into work; approval deep
     links resolve consistently (and reject tampering/expiry).
  8. Tenant deletion removes data across all nine store tiers with a
     zero-residual audit.
  9. Backup/restore round-trips with RTO/RPO measured against objectives.
 10. The load harness reports throughput/latency against targets.
 11. Metrics feed the existing observability event log.
 12. production.* tools are registered, policy-mapped, and the R4 delete
     requires approval (ASK) plus confirm == tenant_id.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from production import new_id
from production.adapters import (
    BackupDirParticipant, BrowserProfileParticipant, MemoryParticipant,
    ObjectStoreParticipant, QueueParticipant, RunLogParticipant,
    SchedulerParticipant, VaultParticipant, VectorIndexParticipant,
)
from production.channels import DeepLinkService, MockMessagingAdapter, BadLink
from production.dr import BackupService, TenantDeletionService
from production.loadtest import LoadHarness, LoadTarget
from production.metrics import Metrics
from production.namespace import ProductionServices, register as register_production
from production.objects import ObjectStore
from production.quotas import QuotaManager, QuotaPolicy
from production.queue import RegionalQueue
from production.routing import (
    IncompatibleFallback, TenantRouter, TenantRoutingPolicy,
)
from production.runlog import RunLogStore
from production.workers import FatalError, RetryableError, WorkerPool
from production.models import WorkItem

from connectors.vault import MemoryVault
from gateway.protocol import (
    Block, ChatMessage, ModelRequest, ModelResponse, ProviderError, TRANSIENT,
)
from gateway.providers.mock import ProgrammableMockProvider
from observability import EventLog
from policy import PolicyEngine, PolicyInput
from tools import ExecutionContext, ToolRegistry

CHECKS: list[tuple[str, bool]] = []


def check(name: str, cond: bool) -> None:
    CHECKS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def expect_raises(name: str, exc_type, fn) -> None:
    try:
        fn()
    except exc_type:
        check(name, True)
    except Exception:
        check(name, False)
    else:
        check(name, False)


ROOT = tempfile.mkdtemp(prefix="openmuse_prod_")


def p(*parts: str) -> str:
    d = os.path.join(ROOT, *parts)
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# 1-4. Queue + workers: submit, execute, idempotent replay, retry
# ---------------------------------------------------------------------------
queue = RegionalQueue(p("queue"), ["us-east", "us-west"])
executed: list[str] = []


def executor(item: WorkItem) -> dict:
    executed.append(item.message_id)
    return {"ok": True, "tokens_used": 10, "provider": "primary"}


item = WorkItem.new(run_id=new_id("run"), tenant_id="t1", kind="turn",
                    payload={"estimated_tokens": 5})
mid, dup = queue.submit(item, region="us-east")
check("submit returns new message id", not dup and mid == item.message_id)

pool = WorkerPool(queue, executor, worker_count=2)
pool.run_until_empty()
got = queue.get(mid)
check("worker completed the run", got.status == "completed")
check("executor ran exactly once", executed == [mid])
check("result recorded", pool.results[mid]["tokens_used"] == 10)

mid2, dup2 = queue.submit(item)
check("replayed message id is a duplicate", dup2 and mid2 == mid)
check("replay did not re-execute", executed == [mid])
check("replay did not resurrect completed state",
      queue.get(mid).status == "completed")

# retryable failure -> bounded retry -> success
flaky_calls: list[str] = []


def flaky(item: WorkItem) -> dict:
    flaky_calls.append(item.message_id)
    if len(flaky_calls) == 1:
        raise RetryableError("transient boom")
    return {"ok": True}


item_c = WorkItem.new(run_id=new_id("run"), tenant_id="t1", kind="turn")
queue.submit(item_c, region="us-west")
WorkerPool(queue, flaky, worker_count=1).run_until_empty()
got_c = queue.get(item_c.message_id)
check("retryable failure retried to success", got_c.status == "completed")
check("retry consumed a bounded attempt", got_c.attempts == 2)

# ---------------------------------------------------------------------------
# 5-7. Regional failure: claim, die, drain, resume exactly once
# ---------------------------------------------------------------------------
item_b = WorkItem.new(run_id=new_id("run"), tenant_id="t1", kind="browser")
queue.submit(item_b, region="us-east")
claimed = queue.claim("us-east", "doomed-worker")
check("doomed worker claimed the message", claimed is not None
      and claimed.message_id == item_b.message_id)
queue.fail_region("us-east")
check("region marked unhealthy", not queue.is_healthy("us-east"))
drain = queue.drain_region("us-east")
check("drain moved in-flight work", drain["moved"] == 1
      and drain["targets"].get("us-west") == 1)

before = list(executed)
WorkerPool(queue, executor, worker_count=2).run_until_empty()
got_b = queue.get(item_b.message_id)
check("failed-over work completed in healthy region",
      got_b.status == "completed" and got_b.region == "us-west")
check("failover executed exactly once",
      executed.count(item_b.message_id) == 1)
check("failover preserved attempt budget", got_b.attempts >= 1)
check("earlier completed runs uncorrupted by failover",
      queue.get(mid).status == "completed"
      and queue.get(item_c.message_id).status == "completed")
queue.heal_region("us-east")
check("region healed", queue.is_healthy("us-east"))
assert executed[len(before):] == [item_b.message_id]

# ---------------------------------------------------------------------------
# 8-11. Quotas: shed, defer, cost accounting
# ---------------------------------------------------------------------------
qm = QuotaManager(p("quotas"))
qm.set_policy("t1", QuotaPolicy(max_calls=2, window_s=3600, on_exceed="shed",
                               unit_cost_per_1k={"primary": 2.0}))
qm.record_usage("t1")
qm.record_usage("t1")
d = qm.check("t1")
check("over-budget tenant is shed", d.action == "shed" and not d.allowed)
check("shed reason names the budget", "call budget" in d.reason)

shed_executed: list[str] = []
pool_q = WorkerPool(queue, lambda i: shed_executed.append(i.message_id) or {},
                    worker_count=1, quota_manager=qm)
item_q = WorkItem.new(run_id=new_id("run"), tenant_id="t1", kind="turn")
queue.submit(item_q, region="us-east")
pool_q.run_until_empty()
got_q = queue.get(item_q.message_id)
check("shed work dead-lettered, never executed",
      got_q.status == "dead" and got_q.dead_reason == "quota_shed"
      and shed_executed == [])

qm.set_policy("t2", QuotaPolicy(max_calls=1, window_s=3600,
                                on_exceed="queue"))
qm.record_usage("t2")
item_d = WorkItem.new(run_id=new_id("run"), tenant_id="t2", kind="turn")
queue.submit(item_d, region="us-east")
WorkerPool(queue, executor, worker_count=1,
           quota_manager=qm).run_until_empty(timeout_s=3)
got_d = queue.get(item_d.message_id)
check("deferred work waits instead of shedding",
      got_d.status == "queued" and item_d.message_id not in executed)
qm.reset("t2")
WorkerPool(queue, executor, worker_count=1,
           quota_manager=qm).run_until_empty()
check("deferred work runs once budget frees",
      queue.get(item_d.message_id).status == "completed"
      and executed.count(item_d.message_id) == 1)

qm.set_policy("t3", QuotaPolicy(unit_cost_per_1k={"primary": 2.0}))
usage = qm.record_usage("t3", tokens=2000, provider="primary")
check("cost accounted per provider unit cost", usage["spend"] == 4.0)
check("quota status reports usage vs budget",
      qm.status("t3")["usage"]["tokens"] == 2000)

# ---------------------------------------------------------------------------
# 12-15. Tenant routing: fallback on transient, never to incompatible
# ---------------------------------------------------------------------------
def _boom(req, hist):
    raise ProviderError(TRANSIENT, "primary down", retryable=True)


primary = ProgrammableMockProvider(_boom)
fallback = ProgrammableMockProvider(
    lambda req, hist: ModelResponse(text="fallback-ok"))
trouter = TenantRouter()
trouter.register_provider("primary", primary,
                          data_policy={"pii_ok": True, "residency": "global"})
trouter.register_provider("fallback", fallback,
                          data_policy={"pii_ok": False, "residency": "global"})
trouter.set_tenant_policy("t1", TenantRoutingPolicy(
    provider_order=["primary", "fallback"]))


def _req(rid: str) -> ModelRequest:
    return ModelRequest(request_id=rid, model_class="fast",
                        messages=[ChatMessage(role="user",
                                              blocks=[Block(kind="text",
                                                            text="hi")])])


resp = trouter.complete("t1", _req("r1"), data_class="public",
                        idempotency_key="k1")
check("transient primary failure falls over", resp.text == "fallback-ok")
check("routing decision names the winner",
      trouter.decisions[-1]["provider"] == "fallback")
check("fallback actually served", len(fallback.calls) == 1)

n_fallback_before = len(fallback.calls)
expect_raises("PII with only an incompatible fallback fails closed",
              ProviderError,
              lambda: trouter.complete("t1", _req("r2"), data_class="pii",
                                      idempotency_key="k2"))
check("PII never reached the incompatible fallback",
      len(fallback.calls) == n_fallback_before)
trouter.set_tenant_policy("t2",
                          TenantRoutingPolicy(provider_order=["fallback"]))
expect_raises("no compatible provider raises IncompatibleFallback",
              IncompatibleFallback,
              lambda: trouter.complete("t2", _req("r3"), data_class="pii",
                                      idempotency_key="k3"))

resp2 = trouter.complete("t1", _req("r1"), data_class="public",
                         idempotency_key="k1")
check("gateway idempotency replays without re-calling",
      resp2.text == "fallback-ok" and len(fallback.calls) == n_fallback_before)

# ---------------------------------------------------------------------------
# 16-19. Messaging adapter + approval deep links
# ---------------------------------------------------------------------------
adapter = MockMessagingAdapter("t1")
in_id = adapter.simulate_inbound("+15551234567", "summarize my day")
work = adapter.inbound_to_work(in_id)
check("inbound message becomes a channel work item",
      work.kind == "channel"
      and work.payload["text"] == "summarize my day")
queue.submit(work, region="us-west")
WorkerPool(queue, executor, worker_count=1).run_until_empty()
check("channel work executed off the queue",
      queue.get(work.message_id).status == "completed")

dl = DeepLinkService(secret=b"test-secret-32-bytes-long!!!!")
link = dl.issue(tenant_id="t1", approval_request_id="apr_123",
                run_id="run_abc")
check("deep link URL shape",
      link.url == f"https://app.openmuse.local/approvals/{link.token}")
resolved = dl.resolve(link.token)
check("deep link resolves to the right approval",
      resolved["approval_request_id"] == "apr_123"
      and resolved["run_id"] == "run_abc"
      and resolved["tenant_id"] == "t1")
expect_raises("tampered deep link rejected", BadLink,
              lambda: dl.resolve(link.token[:-2] + "xx"))
expired = dl.issue(tenant_id="t1", approval_request_id="apr_x",
                   run_id="run_x", ttl_s=-1)
expect_raises("expired deep link rejected", BadLink,
              lambda: dl.resolve(expired.token))

# ---------------------------------------------------------------------------
# 20-27. Disaster recovery + tenant deletion across all tiers
# ---------------------------------------------------------------------------
runlog = RunLogStore(p("runlog"))
objects = ObjectStore(p("objects"))
vault = MemoryVault()


def participants_for(t: str) -> list:
    return [
        RunLogParticipant(runlog),
        ObjectStoreParticipant(objects),
        VectorIndexParticipant(p("vectors")),
        VaultParticipant(vault),
        BrowserProfileParticipant(p("browser_profiles")),
        MemoryParticipant(p("memory")),
        SchedulerParticipant(p("schedules")),
        QueueParticipant(queue),
        BackupDirParticipant(p("backups")),
    ]


def seed(t: str) -> None:
    runlog.append(t, {"event": "run.started", "run_id": "run_seed"})
    objects.put(t, "doc.txt", b"hello production", "text/plain")
    with open(os.path.join(p("vectors"), f"{t}.json"), "w") as f:
        f.write('{"vectors": 3}')
    vault.put(tenant_id=t, provider="github", account_label="me",
              purpose="demo", secret_value="s3cr3t-test-value")
    prof = os.path.join(p("browser_profiles"), t)
    os.makedirs(prof, exist_ok=True)
    with open(os.path.join(prof, "profile.json"), "w") as f:
        f.write('{"cookies": {"sid": "abc"}}')
    mem = os.path.join(p("memory"), t)
    os.makedirs(mem, exist_ok=True)
    with open(os.path.join(mem, "notes.json"), "w") as f:
        f.write('{"notes": ["n1"]}')
    sched = os.path.join(p("schedules"), t)
    os.makedirs(sched, exist_ok=True)
    with open(os.path.join(sched, "schedules.json"), "w") as f:
        f.write('{"sched_1": {"name": "daily"}}')
    queue.submit(WorkItem.new(run_id=new_id("run"), tenant_id=t,
                              kind="scheduled"), region="us-east")


backup = BackupService(p("backups"))
seed("t1")
snap = backup.snapshot_tenant("t1", participants_for("t1"))
check("snapshot covers all nine tiers", len(snap.stores) == 9)
check("snapshot checksummed", snap.sha256.startswith("sha256:"))
check("snapshot file is 0600",
      oct(os.stat(snap.path).st_mode & 0o777) == "0o600")

deletion = TenantDeletionService()
audit = deletion.delete_tenant("t1", participants_for("t1"))
check("tenant deletion audit passed", audit.passed)
check("zero residuals across every tier",
      all(all(v == 0 for v in r.values() if isinstance(v, int))
          for r in audit.residuals.values()))
check("vault material gone", vault.refs_for_tenant("t1") == [])
check("queue messages gone", queue.messages_for_tenant("t1") == [])
check("objects gone", objects.list_keys("t1") == [])
check("run log purged", runlog.count("t1") == 0)

# restore round-trip with RTO/RPO objectives
seed("t1")
snap2 = backup.snapshot_tenant("t1", participants_for("t1"))
objects.delete("t1", "doc.txt")
runlog.purge("t1")
report = backup.restore_tenant("t1", snap2.snapshot_id,
                               participants_for("t1"),
                               rto_target_s=60.0, rpo_target_s=3600.0)
check("restore met RTO/RPO objectives", report.passed)
rd = report.to_dict()
check("restore report carries objective verdicts",
      rd["rto_ok"] and rd["rpo_ok"] and len(rd["stores_restored"]) == 8
      and rd["stores_skipped"] == ["backups"])
_meta, data = objects.get("t1", "doc.txt")
check("object data restored", data == b"hello production")
check("run log restored", runlog.count("t1") == 1)
check("vault credential restored",
      len(vault.refs_for_tenant("t1")) == 1)

# policy-gated backup retention
audit2 = deletion.delete_tenant("t1", participants_for("t1"),
                                purge_backups=False)
check("backups retained under policy",
      audit2.residuals["backups"] == {"residual": "skipped_by_policy"}
      and len(backup.list_snapshots("t1")) > 0)

# ---------------------------------------------------------------------------
# 28-29. Load harness
# ---------------------------------------------------------------------------
def workload() -> None:
    it = WorkItem.new(run_id=new_id("run"), tenant_id="t9", kind="turn")
    queue.submit(it, region="us-west")
    claimed_it = queue.claim("us-west", "load")
    if claimed_it is not None:
        queue.ack("us-west", claimed_it.message_id)


harness = LoadHarness()
rep = harness.run("queue-cycle", workload, concurrency=8, duration_s=1.5,
                  target=LoadTarget(max_p99_ms=2000.0, min_rps=5.0))
check("load test executed calls", rep.calls > 0 and rep.errors == 0)
check("load targets met", rep.passed)
check("report carries latency percentiles",
      rep.p99_ms >= rep.p50_ms >= 0 and rep.throughput_rps > 0)
strict = harness.run("queue-cycle-strict", workload, concurrency=2,
                     duration_s=0.5,
                     target=LoadTarget(max_p99_ms=0.0001, min_rps=1e9))
check("impossible targets fail honestly", not strict.passed)

# ---------------------------------------------------------------------------
# 30-31. Metrics feeding the observability log
# ---------------------------------------------------------------------------
events: list[tuple[str, dict]] = []
elog = EventLog("run_demo")
metrics = Metrics(emit=lambda t, pl: elog.append(t, pl))
metrics.incr("t1", "turns")
metrics.incr("t1", "turns")
metrics.observe("t1", "turn_latency_ms", 12.5)
check("counter accumulated", metrics.counter("t1", "turns") == 2.0)
check("timing stats recorded",
      metrics.timing_stats("t1", "turn_latency_ms")["count"] == 1)
check("metrics emitted to the event log",
      len(elog.of_type("metrics.sample")) == 3)

# ---------------------------------------------------------------------------
# 32-37. production.* tools: registry, policy, approval-bound delete
# ---------------------------------------------------------------------------
registry = ToolRegistry()
services = ProductionServices(
    quotas=qm, backup=backup, deletion=deletion, deeplinks=dl,
    metrics=metrics, channels={"mock-messaging": adapter},
    participants_for=participants_for, exports_root=p("exports"))
register_production(registry, services)
for tool_name in ("production.quota_status", "production.backup_now",
                  "production.request_export", "production.restore",
                  "production.delete_tenant", "production.channel_send"):
    check(f"tool registered: {tool_name}",
          registry.get(tool_name).name == tool_name)

ctx = ExecutionContext(run_id="run_demo", tenant_id="t3",
                       workspace_root=ROOT, event_log=elog)
qs = registry.get("production.quota_status").execute(ctx, {"tenant_id": "t3"})
check("quota_status tool reports usage", qs["usage"]["tokens"] == 2000)

engine = PolicyEngine(os.path.join(os.path.dirname(
    os.path.abspath(__file__)), "policies", "tool-capabilities.yaml"))
check("delete_tenant mapped R4",
      engine.risk_of("production.delete_tenant") == "R4")
dec = engine.evaluate(PolicyInput(
    tool_name="production.delete_tenant", tool_version="1.0.0",
    argument_hash="abc123", risk="R4", capabilities=["production.destroy"],
    side_effect="destructive", has_valid_approval=False))
check("R4 delete without approval fails closed",
      dec.decision == "DENY"
      and dec.reason_code == "APPROVAL_REQUIRED_HIGH_RISK")

expect_raises("delete_tenant refuses mismatched confirm", ValueError,
              lambda: registry.get("production.delete_tenant").execute(
                  ctx, {"tenant_id": "t9", "confirm": "nope"}))
seed("t9")
audit3 = registry.get("production.delete_tenant").execute(
    ctx, {"tenant_id": "t9", "confirm": "t9"})
check("delete_tenant tool returns a clean audit", audit3["passed"])

exp = registry.get("production.request_export").execute(
    ctx, {"tenant_id": "t3"})
check("export bundle written",
      os.path.exists(exp["path"]) and exp["sha256"].startswith("sha256:"))

sent = registry.get("production.channel_send").execute(
    ctx, {"channel": "mock-messaging", "recipient": "+15551234567",
          "text": "your approval is waiting", "deep_link": link.url})
check("channel_send delivered with deep link",
      adapter.outbound[-1]["message_id"] == sent["message_id"]
      and adapter.outbound[-1]["deep_link"] == link.url)

# ---------------------------------------------------------------------------
print(f"\n{sum(1 for _, ok in CHECKS if ok)}/{len(CHECKS)} checks passed")
if not all(ok for _, ok in CHECKS):
    sys.exit(1)
print("ALL CHECKS PASSED")
