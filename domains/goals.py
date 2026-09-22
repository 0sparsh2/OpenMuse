"""Goals domain: goal CRUD, activity entries, attached files, status tracking."""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field


class GoalStatus:
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"

    ALL = (ACTIVE, PAUSED, COMPLETED, ARCHIVED)


@dataclass
class ActivityEntry:
    id: str
    occurred_at: float
    text: str
    source_run_id: str = ""
    source: str = "manual"  # manual | agent | schedule | hook


@dataclass
class Goal:
    id: str
    title: str
    description: str = ""
    status: str = GoalStatus.ACTIVE
    category: str = ""
    target_date: str = ""          # ISO date, optional
    activity: list = field(default_factory=list)   # ActivityEntry dicts
    attachments: list = field(default_factory=list)  # artifact ids
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Goal":
        return Goal(
            id=d["id"], title=d["title"], description=d.get("description", ""),
            status=d.get("status", GoalStatus.ACTIVE),
            category=d.get("category", ""), target_date=d.get("target_date", ""),
            activity=list(d.get("activity", [])),
            attachments=list(d.get("attachments", [])),
            created_at=d.get("created_at", 0.0), updated_at=d.get("updated_at", 0.0))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class GoalStore:
    """File-backed goal store: goals.json under the given root."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._path = os.path.join(root, "goals.json")
        self._goals: dict[str, Goal] = {}
        self._load()

    # -- persistence --------------------------------------------------------
    def _load(self) -> None:
        if os.path.exists(self._path):
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for d in raw.get("goals", []):
                g = Goal.from_dict(d)
                self._goals[g.id] = g

    def _save(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"goals": [g.to_dict() for g in self._goals.values()]},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._path)

    # -- CRUD ---------------------------------------------------------------
    def create(self, *, title: str, description: str = "", category: str = "",
               target_date: str = "") -> Goal:
        if not title.strip():
            raise ValueError("goal title is required")
        g = Goal(id=_new_id("goal"), title=title.strip(),
                 description=description, category=category,
                 target_date=target_date)
        self._goals[g.id] = g
        self._save()
        return g

    def get(self, goal_id: str) -> Goal:
        if goal_id not in self._goals:
            raise KeyError(f"unknown goal: {goal_id!r}")
        return self._goals[goal_id]

    def list(self, *, status: str = "") -> list[Goal]:
        goals = list(self._goals.values())
        if status:
            goals = [g for g in goals if g.status == status]
        return sorted(goals, key=lambda g: g.created_at, reverse=True)

    def update(self, goal_id: str, **fields) -> Goal:
        g = self.get(goal_id)
        allowed = {"title", "description", "category", "target_date"}
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"cannot update field {k!r}")
            setattr(g, k, v)
        g.updated_at = time.time()
        self._save()
        return g

    def set_status(self, goal_id: str, status: str) -> Goal:
        if status not in GoalStatus.ALL:
            raise ValueError(f"invalid goal status: {status!r}")
        g = self.get(goal_id)
        g.status = status
        g.updated_at = time.time()
        self._save()
        return g

    def delete(self, goal_id: str) -> None:
        if goal_id not in self._goals:
            raise KeyError(f"unknown goal: {goal_id!r}")
        del self._goals[goal_id]
        self._save()

    # -- activity -----------------------------------------------------------
    def log_activity(self, goal_id: str, text: str, *, source: str = "manual",
                     source_run_id: str = "") -> ActivityEntry:
        g = self.get(goal_id)
        if not text.strip():
            raise ValueError("activity text is required")
        entry = ActivityEntry(id=_new_id("gact"), occurred_at=time.time(),
                              text=text.strip(), source_run_id=source_run_id,
                              source=source)
        g.activity.append(asdict(entry))
        g.updated_at = time.time()
        self._save()
        return entry

    def attach(self, goal_id: str, artifact_id: str) -> Goal:
        g = self.get(goal_id)
        if artifact_id not in g.attachments:
            g.attachments.append(artifact_id)
            g.updated_at = time.time()
            self._save()
        return g
