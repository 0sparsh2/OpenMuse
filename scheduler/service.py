"""Schedule service — Phase 5.

Implements the `agent.seams.Scheduler` seam and extends it: cron creation
with full options, update (versioned), inspect, remove, enable/disable,
run-now, next-fire preview, and the tick loop that turns due occurrences
into deduplicated job instances.

Authority scoping (blueprint):
- A yes to a one-time task authorizes exactly one run: kind="once"
  schedules fire once and then auto-disable. A second execution without a
  new approval is refused.
- A recurring schedule carries the user's approval text naming the schedule
  (`approval_scope`); every instance snapshots the instruction, ceiling,
  and approval scope at creation.

Editing a schedule creates a NEW version. Already-created job instances
retain their instruction snapshot, so audits remain intelligible.

Misfire policies: skip | fire_once | catch_up (bounded, default cap 10).
Dedup key: schedule_id + scheduled_for + version — a scheduler that
enqueues twice creates at most one instance per key.
"""
from __future__ import annotations

from datetime import datetime
from datetime import timezone as _dt_timezone
from zoneinfo import ZoneInfo

from agent.seams import Scheduler as SchedulerSeam

from .cron_expr import CronExpression, CronError
from .delivery import DeliveryPolicy
from .hooks import HookIngress
from .models import (
    MISFIRE_POLICIES, DeliveryPolicy as _DP, Hook, JobInstance, Schedule, new_id,
)
from .store import ScheduleStore

CATCH_UP_CAP = 10


class ScheduleError(Exception):
    pass


def _parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt_timezone.utc)
    return dt.astimezone(_dt_timezone.utc)


def dedup_key(schedule_id: str, scheduled_for: str, version: int) -> str:
    return f"{schedule_id}+{scheduled_for}+v{version}"


class ScheduleService(SchedulerSeam):
    def __init__(self, store: ScheduleStore):
        self.store = store
        self.ingress = HookIngress(store)

    # -- seam: add_cron / register_hook ----------------------------------
    def add_cron(self, *, name: str, schedule: str, timezone: str, instructions: str) -> str:
        return self.create_schedule(
            name=name, schedule=schedule, timezone=timezone,
            instructions=instructions).schedule_id

    def register_hook(self, *, name: str, event: str, instructions: str) -> str:
        provider, _, event_type = event.partition(".")
        if not provider or not event_type:
            raise ScheduleError(
                f"event must look like 'provider.event_type', got {event!r}")
        return self.create_hook(
            name=name, provider=provider, event_type=event_type,
            instructions=instructions).hook_id

    # -- schedules --------------------------------------------------------
    def create_schedule(self, *, name: str, schedule: str, timezone: str,
                        instructions: str, kind: str = "cron",
                        misfire_policy: str = "skip",
                        capability_ceiling: list | None = None,
                        delivery_policy: dict | None = None,
                        approval_scope: str = "",
                        run_at: str = "",
                        now: datetime | None = None) -> Schedule:
        now = now or datetime.now(_dt_timezone.utc)
        if kind not in ("cron", "once"):
            raise ScheduleError(f"bad kind: {kind!r}")
        if misfire_policy not in MISFIRE_POLICIES:
            raise ScheduleError(f"bad misfire_policy: {misfire_policy!r}")
        try:
            ZoneInfo(timezone)
        except Exception:
            raise ScheduleError(f"unknown timezone: {timezone!r}")
        if kind == "cron":
            try:
                CronExpression(schedule)
            except CronError as e:
                raise ScheduleError(str(e))
            next_fire = CronExpression(schedule).next_fire_after(now, timezone)
            cron_expression, run_at_iso = schedule, ""
        else:
            cron_expression = ""
            run_at_iso = (run_at or now.isoformat())
            next_fire = _parse_utc(run_at_iso)
            if next_fire < now:
                raise ScheduleError("one-shot run_at is in the past")

        sched = Schedule(
            schedule_id=new_id("sch"), name=name, instruction=instructions,
            timezone=timezone, kind=kind, cron_expression=cron_expression,
            run_at=run_at_iso, misfire_policy=misfire_policy,
            capability_ceiling=list(capability_ceiling or []),
            delivery_policy=dict(delivery_policy or _DP().to_dict()),
            approval_scope=approval_scope,
            next_fire_at=next_fire.isoformat(), last_tick_at=now.isoformat(),
        )
        self.store.put_schedule(sched)
        return sched

    def get_schedule(self, schedule_id: str) -> Schedule:
        sched = self.store.get_schedule(schedule_id)
        if not sched:
            raise ScheduleError(f"unknown schedule: {schedule_id!r}")
        return sched

    def list_schedules(self) -> list[Schedule]:
        return self.store.list_schedules()

    def update_schedule(self, schedule_id: str, *, now: datetime | None = None,
                        **changes) -> Schedule:
        """Edit a schedule: bumps the version; fired instances keep snapshots."""
        now = now or datetime.now(_dt_timezone.utc)
        sched = self.get_schedule(schedule_id)
        allowed = {"name", "instruction", "timezone", "cron_expression",
                   "misfire_policy", "capability_ceiling", "delivery_policy",
                   "approval_scope"}
        unknown = set(changes) - allowed
        if unknown:
            raise ScheduleError(f"cannot update: {sorted(unknown)}")
        if "cron_expression" in changes:
            try:
                CronExpression(changes["cron_expression"])
            except CronError as e:
                raise ScheduleError(str(e))
        if "misfire_policy" in changes and changes["misfire_policy"] not in MISFIRE_POLICIES:
            raise ScheduleError(f"bad misfire_policy: {changes['misfire_policy']!r}")
        for k, v in changes.items():
            setattr(sched, k, v)
        sched.version += 1
        sched.updated_at = now.isoformat()
        if sched.kind == "cron":
            sched.next_fire_at = CronExpression(
                sched.cron_expression).next_fire_after(now, sched.timezone).isoformat()
        self.store.put_schedule(sched)
        return sched

    def remove_schedule(self, schedule_id: str) -> None:
        if not self.store.remove_schedule(schedule_id):
            raise ScheduleError(f"unknown schedule: {schedule_id!r}")

    def set_enabled(self, schedule_id: str, enabled: bool) -> Schedule:
        sched = self.get_schedule(schedule_id)
        sched.enabled = enabled
        sched.updated_at = datetime.now(_dt_timezone.utc).isoformat()
        self.store.put_schedule(sched)
        return sched

    def preview(self, schedule_id: str, n: int = 5,
                now: datetime | None = None) -> list[str]:
        """Next n fire times in the schedule's timezone (UI preview)."""
        sched = self.get_schedule(schedule_id)
        now = now or datetime.now(_dt_timezone.utc)
        if sched.kind == "once":
            return [sched.next_fire_at] if sched.enabled else []
        fires = CronExpression(sched.cron_expression).preview(now, sched.timezone, n)
        tz = ZoneInfo(sched.timezone)
        return [f.astimezone(tz).isoformat() for f in fires]

    # -- the tick loop ----------------------------------------------------
    def tick(self, now: datetime | None = None) -> list[JobInstance]:
        """Turn due occurrences into job instances. Idempotent per dedup key."""
        now = now or datetime.now(_dt_timezone.utc)
        fired: list[JobInstance] = []
        for sched in self.store.list_schedules():
            if not sched.enabled:
                continue
            if sched.kind == "once":
                fired.extend(self._tick_once(sched, now))
            else:
                fired.extend(self._tick_cron(sched, now))
        return fired

    def _enqueue(self, *, sched: Schedule, scheduled_for: str,
                 status: str = "queued") -> JobInstance | None:
        key = dedup_key(sched.schedule_id, scheduled_for, sched.version)
        if self.store.has_dedup_key(key):
            return None  # scheduler enqueued twice: still one instance
        inst = JobInstance(
            instance_id=new_id("job"), trigger="schedule",
            schedule_id=sched.schedule_id, scheduled_for=scheduled_for,
            instruction_snapshot=sched.instruction,
            schedule_version=sched.version,
            capability_ceiling=list(sched.capability_ceiling),
            delivery_policy=dict(sched.delivery_policy),
            approval_scope=sched.approval_scope, dedup_key=key, status=status,
        )
        self.store.put_instance(inst)
        return inst

    def _tick_once(self, sched: Schedule, now: datetime) -> list[JobInstance]:
        fired: list[JobInstance] = []
        if _parse_utc(sched.next_fire_at) <= now:
            inst = self._enqueue(sched=sched, scheduled_for=sched.next_fire_at)
            if inst:
                fired.append(inst)
            # A yes to a one-time task authorizes exactly one run.
            sched.enabled = False
            sched.updated_at = now.isoformat()
            self.store.put_schedule(sched)
        return fired

    def _tick_cron(self, sched: Schedule, now: datetime) -> list[JobInstance]:
        fired: list[JobInstance] = []
        cron = CronExpression(sched.cron_expression)
        last = _parse_utc(sched.last_tick_at or sched.created_at)
        # Collect occurrences in (last, now], walking local wall time.
        occurrences: list[datetime] = []
        cursor = last
        guard = 0
        while guard < 10_000:
            nxt = cron.next_fire_after(cursor, sched.timezone)
            if nxt > now:
                break
            occurrences.append(nxt)
            cursor = nxt
            guard += 1
        if occurrences:
            missed, due = occurrences[:-1], occurrences[-1]
            if missed:
                self._apply_misfire(sched, missed, fired)
            inst = self._enqueue(sched=sched, scheduled_for=due.isoformat())
            if inst:
                fired.append(inst)
        sched.last_tick_at = now.isoformat()
        if sched.kind == "cron":
            sched.next_fire_at = cron.next_fire_after(now, sched.timezone).isoformat()
        self.store.put_schedule(sched)
        return fired

    def _apply_misfire(self, sched: Schedule, missed: list[datetime],
                       fired: list[JobInstance]) -> None:
        policy = sched.misfire_policy
        if policy == "skip":
            for m in missed:
                inst = self._enqueue(sched=sched, scheduled_for=m.isoformat(),
                                     status="missed")
                if inst:
                    fired.append(inst)
        elif policy == "fire_once":
            inst = self._enqueue(sched=sched,
                                 scheduled_for=missed[0].isoformat())
            if inst:
                fired.append(inst)
        elif policy == "catch_up":
            for m in missed[:CATCH_UP_CAP]:
                inst = self._enqueue(sched=sched, scheduled_for=m.isoformat())
                if inst:
                    fired.append(inst)

    # -- manual runs ------------------------------------------------------
    def run_now(self, schedule_id: str, *, now: datetime | None = None) -> JobInstance:
        """Execute a schedule once, right now, outside its cadence."""
        now = now or datetime.now(_dt_timezone.utc)
        sched = self.get_schedule(schedule_id)
        if sched.kind == "once" and not sched.enabled:
            raise ScheduleError(
                "one-shot schedule already fired: a one-time approval "
                "authorizes exactly one run")
        inst = self._enqueue(sched=sched, scheduled_for=now.isoformat())
        if inst is None:  # pragma: no cover — same-minute double tap
            raise ScheduleError("a run for this instant already exists")
        if sched.kind == "once":
            sched.enabled = False
            sched.updated_at = now.isoformat()
            self.store.put_schedule(sched)
        return inst

    # -- hooks ------------------------------------------------------------
    def create_hook(self, *, name: str, provider: str, event_type: str,
                    instructions: str, filter: dict | None = None,
                    capability_ceiling: list | None = None,
                    delivery_policy: dict | None = None,
                    signing_secret: str = "", approval_scope: str = "") -> Hook:
        hook = Hook(
            hook_id=new_id("hook"), name=name, provider=provider,
            event_type=event_type, filter=dict(filter or {}),
            instruction=instructions,
            capability_ceiling=list(capability_ceiling or []),
            delivery_policy=dict(delivery_policy or _DP().to_dict()),
            signing_secret_ref=signing_secret, approval_scope=approval_scope,
        )
        self.store.put_hook(hook)
        return hook

    def list_hooks(self) -> list[Hook]:
        return self.store.list_hooks()

    def remove_hook(self, hook_id: str) -> None:
        hooks = {h.hook_id: h for h in self.store.list_hooks()}
        if hook_id not in hooks:
            raise ScheduleError(f"unknown hook: {hook_id!r}")
        del hooks[hook_id]
        # rewrite via put semantics: clear + re-put remaining
        import os
        if os.path.exists(self.store._hooks_path):
            os.unlink(self.store._hooks_path)
        for h in hooks.values():
            self.store.put_hook(h)
