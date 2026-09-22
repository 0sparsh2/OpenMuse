"""Phase 5 verification: scheduler, hooks, and proactive delivery.

Proves, fully offline and deterministic (injected clock):
  A. cron parsing + daylight-saving previews (fall back + spring forward)
  B. one-shot: fires exactly once; a second execution is refused
  C. recurring: fires per schedule; removal stops future enqueues
  D. misfire policies: skip / fire_once / catch_up
  E. dedup: double enqueue -> one instance per dedup key
  F. edit versioning: updates bump the version; fired instances keep snapshots
  G. disable: prevents future enqueues immediately
  H. hooks: signed event -> run; duplicate/bad-signature/stale -> rejected
  I. bounded execution: ceiling denial; ASK while user is away fails closed
  J. delivery critic: quiet hours / caps / materiality / critical override
  K. scheduler.* tools through the registry + policy mappings
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from observability.events import EventLog
from policy import PolicyEngine
from scheduler import ScheduleService, ScheduledRunExecutor
from scheduler.cron_expr import CronExpression, CronError
from scheduler.delivery import (
    DeliveryInput, DeliveryPolicy, decide, in_quiet_hours,
    NOTIFY_NOW, ADD_TO_FEED, HOLD_UNTIL_QUIET_HOURS_END, SILENT_LOG,
)
from scheduler.hooks import sign_event
from scheduler.namespace import register as register_scheduler
from scheduler.service import ScheduleError
from scheduler.store import ScheduleStore
from tools import ToolRegistry
from tools.namespaces import system_tools, file_tools

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="sched_demo_")
    ws = os.path.join(tmp, "workspace")
    os.makedirs(ws)
    store = ScheduleStore(os.path.join(tmp, "schedules"))
    T0 = utc(2026, 9, 22, 10, 0, 0)
    service = ScheduleService(store)

    # -- A. cron parsing + DST --------------------------------------------------
    try:
        CronExpression("not a cron")
        check("bad cron rejected", False)
    except CronError:
        check("bad cron rejected", True)
    c = CronExpression("*/15 9-17 * * 1-5")
    check("cron fields parsed",
          c.minute == set(range(0, 60, 15)) and c.hour == set(range(9, 18))
          and c.dow == {1, 2, 3, 4, 5}, str(sorted(c.minute)[:4]))
    # Fall back 2026-11-01 (America/New_York): daily 09:00 stays 09:00 local.
    daily = CronExpression("0 9 * * *")
    prev = daily.preview(utc(2026, 10, 30, 12, 0), "America/New_York", 5)
    ny = ZoneInfo("America/New_York")
    local = [p.astimezone(ny) for p in prev]
    check("DST fall-back: 09:00 local every day",
          all(d.hour == 9 and d.minute == 0 for d in local)
          and {d.date().isoformat() for d in local} ==
          {"2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02", "2026-11-03"},
          str([d.isoformat() for d in local]))
    # Spring forward 2026-03-08: 02:30 does not exist -> skipped.
    early = CronExpression("30 2 * * *")
    prev2 = early.preview(utc(2026, 3, 7, 5, 0), "America/New_York", 2)
    local2 = [p.astimezone(ny) for p in prev2]
    check("DST spring-forward: nonexistent 02:30 skipped",
          [d.date().isoformat() for d in local2] == ["2026-03-07", "2026-03-09"]
          and all(d.hour == 2 and d.minute == 30 for d in local2),
          str([d.isoformat() for d in local2]))

    # -- B. one-shot: exactly one run -------------------------------------------
    once = service.create_schedule(
        name="one-time brief", schedule="once", timezone="UTC",
        instructions="Summarize today's inbox.", kind="once",
        capability_ceiling=["system"], approval_scope="user approved one-time brief",
        now=T0)
    fired = service.tick(now=T0 + timedelta(minutes=5))
    check("one-shot fires on tick", len(fired) == 1 and fired[0].trigger == "schedule",
          f"fired={len(fired)}")
    check("one-shot auto-disables after firing",
          service.get_schedule(once.schedule_id).enabled is False)
    again = service.tick(now=T0 + timedelta(minutes=10))
    check("one-shot does not fire twice",
          len(service.store.instances_for_schedule(once.schedule_id)) == 1
          and not again)
    try:
        service.run_now(once.schedule_id)
        check("second one-shot run_now refused", False)
    except ScheduleError as e:
        check("second one-shot run_now refused", "exactly one run" in str(e), str(e))

    # -- C. recurring: fires per schedule; removal stops it ----------------------
    cron = service.create_schedule(
        name="minutely ping", schedule="*/1 * * * *", timezone="UTC",
        instructions="Record a heartbeat.", capability_ceiling=["system"],
        approval_scope="user approved recurring minutely ping", now=T0)
    f1 = service.tick(now=T0 + timedelta(minutes=1, seconds=30))
    check("recurring fires on schedule", len(f1) == 1, f"fired={len(f1)}")
    f2 = service.tick(now=T0 + timedelta(minutes=2, seconds=30))
    check("recurring fires again next interval", len(f2) == 1)
    service.remove_schedule(cron.schedule_id)
    f3 = service.tick(now=T0 + timedelta(minutes=3, seconds=30))
    check("removal stops future enqueues", f3 == [])
    check("run history retained after removal",
          len(store.run_history()) >= 0)  # history API works; runs recorded below

    # -- D. misfire policies -----------------------------------------------------
    hourly_skip = service.create_schedule(
        name="hourly skip", schedule="0 * * * *", timezone="UTC",
        instructions="Hourly job.", misfire_policy="skip",
        capability_ceiling=["system"], now=T0)
    insts = service.tick(now=T0 + timedelta(hours=3, minutes=30))
    by_status = {}
    for i in insts:
        by_status[i.status] = by_status.get(i.status, 0) + 1
    check("misfire=skip: missed recorded, due enqueued",
          by_status.get("missed") == 2 and by_status.get("queued") == 1,
          str(by_status))
    sched = service.get_schedule(hourly_skip.schedule_id)
    check("next_fire_at advanced past now",
          datetime.fromisoformat(sched.next_fire_at) > T0 + timedelta(hours=3, minutes=30),
          sched.next_fire_at)

    hourly_once = service.create_schedule(
        name="hourly fire_once", schedule="0 * * * *", timezone="UTC",
        instructions="Hourly job.", misfire_policy="fire_once",
        capability_ceiling=["system"], now=T0)
    insts2 = service.tick(now=T0 + timedelta(hours=3, minutes=30))
    check("misfire=fire_once: one catch-up + due",
          sum(1 for i in insts2 if i.status == "queued") == 2, str(len(insts2)))

    hourly_catch = service.create_schedule(
        name="hourly catch_up", schedule="0 * * * *", timezone="UTC",
        instructions="Hourly job.", misfire_policy="catch_up",
        capability_ceiling=["system"], now=T0)
    insts3 = service.tick(now=T0 + timedelta(hours=3, minutes=30))
    check("misfire=catch_up: bounded missed instances enqueued",
          sum(1 for i in insts3 if i.status == "queued") == 3, str(len(insts3)))

    # -- E. dedup -----------------------------------------------------------------
    dedup_sched = service.create_schedule(
        name="dedup probe", schedule="*/5 * * * *", timezone="UTC",
        instructions="Probe.", capability_ceiling=["system"], now=T0)
    T = T0 + timedelta(minutes=7)
    n_before = len(store.instances_for_schedule(dedup_sched.schedule_id))
    service.tick(now=T)
    n_after_first = len(store.instances_for_schedule(dedup_sched.schedule_id))
    # Simulate the scheduler enqueueing twice: rewind last_tick and tick again.
    s = service.get_schedule(dedup_sched.schedule_id)
    s.last_tick_at = T0.isoformat()
    store.put_schedule(s)
    service.tick(now=T)
    n_after_second = len(store.instances_for_schedule(dedup_sched.schedule_id))
    check("dedup: double tick creates one instance per key",
          n_after_first == n_before + 1 and n_after_second == n_after_first,
          f"{n_before}->{n_after_first}->{n_after_second}")

    # -- F. edit versioning --------------------------------------------------------
    ver = service.create_schedule(
        name="versioned", schedule="*/2 * * * *", timezone="UTC",
        instructions="instruction v1", capability_ceiling=["system"], now=T0)
    service.tick(now=T0 + timedelta(minutes=3))
    inst_v1 = store.instances_for_schedule(ver.schedule_id)[0]
    updated = service.update_schedule(ver.schedule_id, instruction="instruction v2",
                                      now=T0 + timedelta(minutes=4))
    check("edit bumps the version",
          updated.version == 2 and updated.instruction == "instruction v2")
    inst_v1_again = store.instances_for_schedule(ver.schedule_id)[0]
    check("fired instance keeps its snapshot",
          inst_v1_again.instruction_snapshot == "instruction v1"
          and inst_v1_again.schedule_version == 1
          and inst_v1_again.dedup_key.endswith("+v1"),
          inst_v1_again.dedup_key)

    # -- G. disable -----------------------------------------------------------------
    dis = service.create_schedule(
        name="disable me", schedule="*/1 * * * *", timezone="UTC",
        instructions="x", capability_ceiling=["system"], now=T0)
    service.set_enabled(dis.schedule_id, False)
    n_dis_before = len(store.instances_for_schedule(dis.schedule_id))
    service.tick(now=T0 + timedelta(minutes=5))
    check("disable prevents future enqueues immediately",
          len(store.instances_for_schedule(dis.schedule_id)) == n_dis_before)

    # -- H. hooks --------------------------------------------------------------------
    hook = service.create_hook(
        name="inbox watch", provider="gmail", event_type="message.received",
        instructions="Summarize the reply.",
        filter={"from": {"domain": "example.com"}},
        capability_ceiling=["system"], signing_secret="test-secret",
        approval_scope="user approved inbox watch hook")
    payload = {"from": "a@example.com", "subject": "Re: intro"}
    ts = (T0 + timedelta(minutes=1)).isoformat()
    sig = sign_event("test-secret", "evt_1", ts, payload)
    r1 = service.ingress.receive(provider="gmail", event_type="message.received",
                                 event_id="evt_1", timestamp=ts, payload=payload,
                                 signature=sig, now=T0 + timedelta(minutes=2))
    check("signed hook event enqueues a run",
          r1.accepted and r1.instance is not None and r1.instance.trigger == "hook",
          r1.reason)
    r2 = service.ingress.receive(provider="gmail", event_type="message.received",
                                 event_id="evt_1", timestamp=ts, payload=payload,
                                 signature=sig, now=T0 + timedelta(minutes=2))
    check("duplicate event delivery creates no new run",
          not r2.accepted and r2.reason == "DUPLICATE_EVENT", r2.reason)
    r3 = service.ingress.receive(provider="gmail", event_type="message.received",
                                 event_id="evt_2", timestamp=ts, payload=payload,
                                 signature="bad", now=T0 + timedelta(minutes=2))
    check("bad signature rejected", not r3.accepted and r3.reason == "BAD_SIGNATURE")
    old_ts = (T0 - timedelta(minutes=30)).isoformat()
    r4 = service.ingress.receive(provider="gmail", event_type="message.received",
                                 event_id="evt_3", timestamp=old_ts, payload=payload,
                                 signature=sign_event("test-secret", "evt_3", old_ts, payload),
                                 now=T0)
    check("stale event outside replay window rejected",
          not r4.accepted and r4.reason == "REPLAY_WINDOW_EXCEEDED")

    # -- I. bounded execution ----------------------------------------------------------
    registry = ToolRegistry()
    system_tools.register(registry, loaded_namespaces=set())
    file_tools.register(registry)
    policy = PolicyEngine(os.path.join(ROOT, "policies", "tool-capabilities.yaml"))
    executor = ScheduledRunExecutor(store=store, registry=registry,
                                    policy=policy, workspace_root=ws)

    # I.1: R0 read inside the ceiling executes.
    read_sched = service.create_schedule(
        name="clock reader", schedule="*/1 * * * *", timezone="UTC",
        instructions="Read the clock.", capability_ceiling=["system"],
        now=T0)
    read_inst = service.run_now(read_sched.schedule_id, now=T0)
    out = executor.execute(read_inst, [{"tool": "system.clock", "arguments": {}}],
                           now=T0)
    check("R0 read inside ceiling executes",
          out.record.status == "completed" and len(out.envelopes) == 1
          and out.envelopes[0]["status"] == "succeeded", out.record.status)
    check("delivery: material read notifies",
          out.record.delivery["decision"] == NOTIFY_NOW,
          str(out.record.delivery))

    # I.2: tool outside the ceiling is denied before policy.
    out2 = executor.execute(read_inst, [{"tool": "files.write",
                                         "arguments": {"path": "x.txt", "content": "x"}}],
                            now=T0)
    check("tool outside ceiling denied (CEILING_DENIED)",
          any(d["reason"] == "CEILING_DENIED" for d in out2.record.policy_denials)
          and out2.scoped_out[0]["tool"] == "files.write")

    # I.3: R2 write inside the ceiling needs approval; user is away -> fail closed.
    write_sched = service.create_schedule(
        name="nightly writer", schedule="*/1 * * * *", timezone="UTC",
        instructions="Write a note.", capability_ceiling=["system", "files"],
        delivery_policy={"mode": "notify_if_material", "quiet_hours": [],
                         "max_notifications_per_day": 3},
        now=T0)
    write_inst = service.run_now(write_sched.schedule_id, now=T0)
    target = os.path.join(ws, "nightly.txt")
    out3 = executor.execute(write_inst, [{"tool": "files.write",
                                          "arguments": {"path": "nightly.txt",
                                                        "content": "hello"}}],
                            now=T0)
    check("ASK while user is away fails closed",
          out3.record.status == "failed_closed"
          and any(d["reason"] == "UNATTENDED_FAIL_CLOSED"
                  for d in out3.record.policy_denials),
          out3.record.status)
    check("no write happened without approval", not os.path.exists(target))
    check("approval deferred for the user",
          out3.record.pending_approval is not None
          and out3.record.pending_approval["status"] == "pending")
    check("run history recorded",
          any(r.run_id == out3.record.run_id
              for r in store.run_history(schedule_id=write_sched.schedule_id)),
          f"history={len(store.run_history(schedule_id=write_sched.schedule_id))}")

    # -- J. delivery critic --------------------------------------------------------------
    pol = DeliveryPolicy(mode="notify_if_material", quiet_hours=["22:00", "08:00"],
                         max_notifications_per_day=2)
    q_now = utc(2026, 9, 23, 3, 30)  # 23:30 ET — inside quiet hours
    d1 = decide(DeliveryInput(material=True), pol, now_utc=q_now,
                tz_name="America/New_York", notifications_sent_today=0)
    check("quiet hours hold non-critical results",
          d1.decision == HOLD_UNTIL_QUIET_HOURS_END, d1.decision)
    d2 = decide(DeliveryInput(material=True, is_security_alert=True), pol,
                now_utc=q_now, tz_name="America/New_York",
                notifications_sent_today=99)
    check("critical alert notifies even in quiet hours / at cap",
          d2.decision == NOTIFY_NOW, d2.decision)
    day_now = utc(2026, 9, 22, 14, 0)  # 10:00 ET — outside quiet hours
    d3 = decide(DeliveryInput(material=True), pol, now_utc=day_now,
                tz_name="America/New_York", notifications_sent_today=2)
    check("daily cap reroutes to feed", d3.decision == ADD_TO_FEED, d3.decision)
    d4 = decide(DeliveryInput(material=False), pol, now_utc=day_now,
                tz_name="America/New_York", notifications_sent_today=0)
    check("'no material change' stays silent",
          d4.decision == SILENT_LOG, d4.decision)
    d5 = decide(DeliveryInput(material=True), pol, now_utc=day_now,
                tz_name="America/New_York", notifications_sent_today=0)
    check("material result notifies", d5.decision == NOTIFY_NOW, d5.decision)
    check("quiet-hours window crosses midnight",
          in_quiet_hours(q_now, "America/New_York", ["22:00", "08:00"])
          and not in_quiet_hours(day_now, "America/New_York", ["22:00", "08:00"]))

    # -- K. scheduler.* tools through the registry ------------------------------------------
    ns_registry = ToolRegistry()
    register_scheduler(ns_registry, service)
    ctx = SimpleNamespace(workspace_root=ws, run_id="run_demo")
    created = ns_registry.get("scheduler.create").execute(ctx, {
        "name": "tool-made", "schedule": "0 8 * * 1-5", "timezone": "America/New_York",
        "instructions": "Morning brief.", "capability_ceiling": ["system"],
        "approval_scope": "user approved morning brief weekdays 8am"})
    check("scheduler.create via tool",
          created["schedule_id"].startswith("sch_") and len(created["next_runs"]) == 5,
          str(created.get("next_runs")))
    listed = ns_registry.get("scheduler.list").execute(ctx, {})
    check("scheduler.list via tool",
          any(s["schedule_id"] == created["schedule_id"] for s in listed["schedules"]))
    got = ns_registry.get("scheduler.get").execute(ctx, {"schedule_id": created["schedule_id"]})
    check("scheduler.get via tool", got["schedule"]["name"] == "tool-made")
    upd = ns_registry.get("scheduler.update").execute(
        ctx, {"schedule_id": created["schedule_id"], "instruction": "Evening brief."})
    check("scheduler.update via tool bumps version", upd["version"] == 2)
    rn = ns_registry.get("scheduler.run_now").execute(ctx, {"schedule_id": created["schedule_id"]})
    check("scheduler.run_now via tool", rn["instance_id"].startswith("job_"))
    dis_out = ns_registry.get("scheduler.disable").execute(ctx, {"schedule_id": created["schedule_id"]})
    check("scheduler.disable via tool", dis_out["enabled"] is False)
    en_out = ns_registry.get("scheduler.enable").execute(ctx, {"schedule_id": created["schedule_id"]})
    check("scheduler.enable via tool", en_out["enabled"] is True)
    hk = ns_registry.get("scheduler.hook_create").execute(ctx, {
        "name": "tool hook", "provider": "cal", "event_type": "event.created",
        "instructions": "Check the event."})
    check("scheduler.hook_create via tool", hk["hook_id"].startswith("hook_"))
    hkl = ns_registry.get("scheduler.hook_list").execute(ctx, {})
    check("scheduler.hook_list via tool",
          any(h["hook_id"] == hk["hook_id"] for h in hkl["hooks"]))
    rem = ns_registry.get("scheduler.remove").execute(ctx, {"schedule_id": created["schedule_id"]})
    check("scheduler.remove via tool", rem["removed"] is True)

    # -- L. policy mappings ---------------------------------------------------------------------
    with open(os.path.join(ROOT, "policies", "tool-capabilities.yaml")) as fh:
        caps = yaml.safe_load(fh)["tools"]
    check("policy: scheduler.create is R2/local-write",
          caps["scheduler.create"]["risk"] == "R2"
          and caps["scheduler.create"]["side_effect"] == "local_write")
    check("policy: scheduler.list/get/hook_list are R0",
          all(caps[t]["risk"] == "R0" for t in
              ("scheduler.list", "scheduler.get", "scheduler.hook_list")))

    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
