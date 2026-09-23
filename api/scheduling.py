"""
Per-user schedules in the live app (issue #4).

Each user has their own ScheduleService (files under
users/<user_id>/scheduler/). A runner thread ticks every user's schedules;
every due job becomes an ordinary run submitted *as the owning user* into
that user's "Scheduled" chat — same policy, approvals, budgets and memory as
a typed message (there is no privileged background mode). When the run ends
the job instance is marked completed/failed and the user is notified.
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timezone as _tz

from scheduler.service import ScheduleService
from scheduler.store import ScheduleStore

TICK_SECONDS = 15
_DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def _safe(uid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", uid)[:80] or "user"


def describe_cron(expr: str) -> str:
    """Plain-English cadence for the common cron shapes; falls back to the expression."""
    parts = (expr or "").split()
    if len(parts) != 5:
        return expr
    mi, hr, dom, mon, dow = parts
    at = ""
    if mi.isdigit() and hr.isdigit():
        h, m = int(hr), int(mi)
        at = f" at {((h + 11) % 12) + 1}:{m:02d} {'AM' if h < 12 else 'PM'}"
    if mi.startswith("*/") and hr == "*" and dom == mon == dow == "*":
        return f"Every {mi[2:]} minutes"
    if mi.isdigit() and hr == "*" and dom == mon == dow == "*":
        return f"Every hour at :{int(mi):02d}"
    if dom == "*" and mon == "*":
        if dow == "*":
            return "Every day" + at
        if dow in ("1-5", "MON-FRI"):
            return "Every weekday" + at
        if dow in ("0,6", "6,0", "SAT,SUN"):
            return "Every weekend day" + at
        if dow.isdigit() and 0 <= int(dow) <= 7:
            return f"Every {_DAYS[int(dow) % 7]}" + at
        if re.fullmatch(r"[0-7](,[0-7])+", dow):
            return "Every " + ", ".join(_DAYS[int(d) % 7] for d in dow.split(",")) + at
    if dom.isdigit() and mon == "*" and dow == "*":
        n = int(dom)
        suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"Monthly on the {n}{suffix}" + at
    return expr


class UserSchedules:
    def __init__(self, backend, root: str):
        self.backend = backend
        self.root = root
        self._services: dict[str, ScheduleService] = {}
        self._lock = threading.RLock()
        self._running: dict[str, tuple[str, str, str]] = {}  # run_id -> (uid, schedule_id, instance_id)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- per-user services ----------------------------------------------------
    def service_for(self, user_id: str) -> ScheduleService:
        uid = _safe(user_id or "user_api")
        with self._lock:
            svc = self._services.get(uid)
            if svc is None:
                svc = self._services[uid] = ScheduleService(
                    ScheduleStore(os.path.join(self.root, uid, "scheduler")))
                svc.user_id = user_id or "user_api"
            return svc

    def _all_services(self) -> list[ScheduleService]:
        if os.path.isdir(self.root):
            for uid in os.listdir(self.root):
                if os.path.isdir(os.path.join(self.root, uid, "scheduler")):
                    self.service_for(uid)
        with self._lock:
            return list(self._services.values())

    # -- runner -----------------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick_all()
            except Exception as exc:  # the runner must never die
                print(f"scheduler tick failed: {exc}", flush=True)

    def tick_all(self, now: datetime | None = None) -> list[str]:
        started = []
        for svc in self._all_services():
            for inst in svc.tick(now or datetime.now(_tz.utc)):
                if inst.status == "queued":
                    rid = self._fire(svc, inst)
                    if rid:
                        started.append(rid)
        return started

    def run_now(self, user_id: str, schedule_id: str) -> str:
        svc = self.service_for(user_id)
        inst = svc.run_now(schedule_id)
        return self._fire(svc, inst)

    def _scheduled_chat(self, user_id: str) -> str:
        marker = os.path.join(self.root, _safe(user_id), "scheduler", "chat_id")
        if os.path.exists(marker):
            with open(marker, encoding="utf-8") as fh:
                chat_id = fh.read().strip()
            if chat_id in self.backend.sessions:
                return chat_id
        rec = self.backend.create_session(user_id=user_id, title="Scheduled")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(rec.chat_id)
        return rec.chat_id

    def _fire(self, svc: ScheduleService, inst) -> str:
        uid = svc.user_id
        sched = svc.store.get_schedule(inst.schedule_id) if inst.schedule_id else None
        name = sched.name if sched else "Scheduled task"
        chat_id = self._scheduled_chat(uid)
        text = (f"[Scheduled task “{name}” — this runs on its own schedule and the user may not be "
                f"watching. Do the task, then report the result briefly.]\n{inst.instruction_snapshot}")
        run, created, _ = self.backend.submit_message(
            chat_id=chat_id, user_id=uid, content=[{"type": "text", "text": text}],
            idempotency_key="sched:" + inst.dedup_key)
        inst.status = "running"
        svc.store.put_instance(inst)
        self._running[run.run_id] = (uid, inst.schedule_id, inst.instance_id)
        return run.run_id

    def on_run_finished(self, run) -> None:
        """Called by the backend when any run ends; closes out scheduled jobs."""
        entry = self._running.pop(run.run_id, None)
        if entry is None:
            return
        uid, schedule_id, instance_id = entry
        svc = self.service_for(uid)
        for inst in svc.store.instances_for_schedule(schedule_id):
            if inst.instance_id == instance_id:
                inst.status = "completed" if run.state == "COMPLETED" else "failed"
                svc.store.put_instance(inst)
        sched = svc.store.get_schedule(schedule_id)
        notify = getattr(self.backend, "notify", None)
        if notify is not None:
            ok = run.state == "COMPLETED"
            notify(uid, kind="schedule", title=f"{sched.name if sched else 'Scheduled task'} "
                                               f"{'finished' if ok else 'failed'}",
                   body=(run.final_text or run.failure_message or "")[:280],
                   link={"chat_id": run.chat_id, "run_id": run.run_id},
                   dedupe_key=f"schedule:{run.run_id}")

    # -- views --------------------------------------------------------------------
    def view(self, svc: ScheduleService, sched) -> dict:
        d = sched.to_dict()
        d["cadence"] = (describe_cron(sched.cron_expression) if sched.kind == "cron"
                        else "Once")
        try:
            d["next_runs"] = svc.preview(sched.schedule_id, n=5) if sched.enabled else []
        except Exception:
            d["next_runs"] = []
        return d
