"""Scheduler data models — Phase 5.

Mirrors the blueprint's cron schema, job-instance payload, and hook
subscription contract. All models are plain dataclasses with to_dict /
from_dict so the file-backed store can persist them as JSON.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


MISFIRE_POLICIES = ("skip", "fire_once", "catch_up")
SCHEDULE_KINDS = ("cron", "once")
DELIVERY_MODES = ("notify_if_material", "always_notify", "silent")


@dataclass
class DeliveryPolicy:
    mode: str = "notify_if_material"  # notify_if_material | always_notify | silent
    quiet_hours: list = field(default_factory=list)  # ["22:00", "08:00"] local
    max_notifications_per_day: int = 3

    def to_dict(self) -> dict:
        return {"mode": self.mode, "quiet_hours": list(self.quiet_hours),
                "max_notifications_per_day": self.max_notifications_per_day}

    @classmethod
    def from_dict(cls, d: dict) -> "DeliveryPolicy":
        mode = d.get("mode", "notify_if_material")
        if mode not in DELIVERY_MODES:
            raise ValueError(f"bad delivery mode: {mode!r}")
        return cls(mode=mode, quiet_hours=list(d.get("quiet_hours", [])),
                   max_notifications_per_day=int(d.get("max_notifications_per_day", 3)))


@dataclass
class Schedule:
    """A cron (or one-shot) schedule. Edits create a new version."""
    schedule_id: str
    name: str
    instruction: str
    timezone: str
    kind: str = "cron"               # cron | once
    cron_expression: str = ""        # required when kind == cron
    run_at: str = ""                 # ISO UTC, for kind == once
    misfire_policy: str = "skip"
    capability_ceiling: list = field(default_factory=list)
    delivery_policy: dict = field(default_factory=dict)
    enabled: bool = True
    version: int = 1
    approval_scope: str = ""         # the user's approval text naming the schedule
    next_fire_at: str = ""           # ISO UTC of next occurrence
    last_tick_at: str = ""           # ISO UTC of last tick processed
    created_at: str = field(default_factory=_utcnow)
    updated_at: str = field(default_factory=_utcnow)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "schedule_id", "name", "instruction", "timezone", "kind",
            "cron_expression", "run_at", "misfire_policy", "capability_ceiling",
            "delivery_policy", "enabled", "version", "approval_scope",
            "next_fire_at", "last_tick_at", "created_at", "updated_at")}

    @classmethod
    def from_dict(cls, d: dict) -> "Schedule":
        import dataclasses
        kwargs = {}
        for k, f in cls.__dataclass_fields__.items():
            if k in d:
                kwargs[k] = d[k]
            elif f.default is not dataclasses.MISSING:
                kwargs[k] = f.default
            elif f.default_factory is not dataclasses.MISSING:  # type: ignore
                kwargs[k] = f.default_factory()  # type: ignore
        return cls(**kwargs)


@dataclass
class JobInstance:
    """One enqueued fire of a schedule. Instruction is a snapshot — edits to
    the schedule (new versions) never mutate already-created instances."""
    instance_id: str
    trigger: str                    # "schedule" | "hook" | "manual"
    schedule_id: str = ""
    hook_id: str = ""
    scheduled_for: str = ""         # ISO UTC of the occurrence
    instruction_snapshot: str = ""
    schedule_version: int = 1
    capability_ceiling: list = field(default_factory=list)
    delivery_policy: dict = field(default_factory=dict)
    approval_scope: str = ""
    dedup_key: str = ""             # schedule_id + scheduled_for + version
    status: str = "queued"          # queued | running | completed | failed |
                                    # missed | failed_closed | waiting_approval | denied
    created_at: str = field(default_factory=_utcnow)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, d: dict) -> "JobInstance":
        return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})


@dataclass
class Hook:
    hook_id: str
    name: str
    provider: str
    event_type: str
    filter: dict = field(default_factory=dict)
    instruction: str = ""
    capability_ceiling: list = field(default_factory=list)
    delivery_policy: dict = field(default_factory=dict)
    signing_secret_ref: str = ""    # vault ref in production; raw test secret in demo only
    approval_scope: str = ""
    enabled: bool = True
    created_at: str = field(default_factory=_utcnow)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, d: dict) -> "Hook":
        return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})


@dataclass
class RunRecord:
    """Durable history entry for one executed (or attempted) instance."""
    run_id: str
    instance_id: str
    dedup_key: str
    trigger: str
    status: str
    started_at: str
    schedule_id: str = ""
    hook_id: str = ""
    finished_at: str = ""
    tool_calls: list = field(default_factory=list)   # [{tool, arguments, decision, ...}]
    policy_denials: list = field(default_factory=list)
    pending_approval: Optional[dict] = None
    delivery: Optional[dict] = None                  # {decision, rationale}
    result_summary: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, d: dict) -> "RunRecord":
        return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})
