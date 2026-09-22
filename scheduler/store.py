"""Durable scheduler state — Phase 5.

File-backed, one tenant per root directory (mirrors the memory store's
per-tenant layout). Schedules, instances, hooks, and run history are JSON
files written atomically (tmp + rename); hook event IDs are a JSON set for
deduplication.

Optimistic versioning: Schedule.version increments on every edit; job
instances pin the version they were created from.
"""
from __future__ import annotations

import json
import os
import tempfile

from .models import Hook, JobInstance, RunRecord, Schedule


def _atomic_write(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class ScheduleStore:
    """Durable job state and run history for one tenant."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(self.root, exist_ok=True)
        self._schedules_path = os.path.join(root, "schedules.json")
        self._instances_path = os.path.join(root, "instances.json")
        self._hooks_path = os.path.join(root, "hooks.json")
        self._runs_path = os.path.join(root, "runs.jsonl")
        self._events_path = os.path.join(root, "seen_event_ids.json")
        self._notify_path = os.path.join(root, "notification_counts.json")

    # -- schedules ------------------------------------------------------
    def _load_schedules(self) -> dict:
        return _read_json(self._schedules_path, {})

    def _save_schedules(self, data: dict) -> None:
        _atomic_write(self._schedules_path, data)

    def put_schedule(self, schedule: Schedule) -> None:
        data = self._load_schedules()
        data[schedule.schedule_id] = schedule.to_dict()
        self._save_schedules(data)

    def get_schedule(self, schedule_id: str) -> Schedule | None:
        data = self._load_schedules()
        raw = data.get(schedule_id)
        return Schedule.from_dict(raw) if raw else None

    def list_schedules(self) -> list[Schedule]:
        data = self._load_schedules()
        return [Schedule.from_dict(v) for v in data.values()]

    def remove_schedule(self, schedule_id: str) -> bool:
        data = self._load_schedules()
        if schedule_id not in data:
            return False
        del data[schedule_id]
        self._save_schedules(data)
        return True

    # -- instances ------------------------------------------------------
    def _load_instances(self) -> dict:
        return _read_json(self._instances_path, {})

    def _save_instances(self, data: dict) -> None:
        _atomic_write(self._instances_path, data)

    def put_instance(self, instance: JobInstance) -> None:
        data = self._load_instances()
        data[instance.instance_id] = instance.to_dict()
        self._save_instances(data)

    def has_dedup_key(self, dedup_key: str) -> bool:
        return any(i.get("dedup_key") == dedup_key
                   for i in self._load_instances().values())

    def instances_for_schedule(self, schedule_id: str) -> list[JobInstance]:
        return [JobInstance.from_dict(v) for v in self._load_instances().values()
                if v.get("schedule_id") == schedule_id]

    # -- hooks ----------------------------------------------------------
    def _load_hooks(self) -> dict:
        return _read_json(self._hooks_path, {})

    def _save_hooks(self, data: dict) -> None:
        _atomic_write(self._hooks_path, data)

    def put_hook(self, hook: Hook) -> None:
        data = self._load_hooks()
        data[hook.hook_id] = hook.to_dict()
        self._save_hooks(data)

    def get_hook(self, hook_id: str) -> Hook | None:
        data = self._load_hooks()
        raw = data.get(hook_id)
        return Hook.from_dict(raw) if raw else None

    def list_hooks(self) -> list[Hook]:
        data = self._load_hooks()
        return [Hook.from_dict(v) for v in data.values()]

    def hooks_for(self, provider: str, event_type: str) -> list[Hook]:
        return [h for h in self.list_hooks()
                if h.enabled and h.provider == provider and h.event_type == event_type]

    # -- run history ----------------------------------------------------
    def append_run(self, record: RunRecord) -> None:
        os.makedirs(os.path.dirname(self._runs_path), exist_ok=True)
        with open(self._runs_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    def run_history(self, *, schedule_id: str = "", hook_id: str = "",
                    limit: int = 50) -> list[RunRecord]:
        if not os.path.exists(self._runs_path):
            return []
        out = []
        with open(self._runs_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                if schedule_id and raw.get("schedule_id") != schedule_id:
                    continue
                if hook_id and raw.get("hook_id") != hook_id:
                    continue
                out.append(RunRecord.from_dict(raw))
        return out[-limit:]

    # -- hook event dedup -----------------------------------------------
    def seen_event(self, event_id: str) -> bool:
        return event_id in _read_json(self._events_path, [])

    def mark_event_seen(self, event_id: str) -> None:
        seen = set(_read_json(self._events_path, []))
        seen.add(event_id)
        _atomic_write(self._events_path, sorted(seen))

    # -- notification caps ----------------------------------------------
    def notifications_today(self, day: str) -> int:
        return int(_read_json(self._notify_path, {}).get(day, 0))

    def record_notification(self, day: str) -> int:
        counts = _read_json(self._notify_path, {})
        counts[day] = int(counts.get(day, 0)) + 1
        _atomic_write(self._notify_path, counts)
        return counts[day]
