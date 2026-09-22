"""Ideas domain: quick idea capture, lists, promotion into goals."""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field


@dataclass
class Idea:
    id: str
    text: str
    title: str = ""
    status: str = "open"        # open | promoted | archived
    promoted_goal_id: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Idea":
        return Idea(id=d["id"], text=d["text"], title=d.get("title", ""),
                    status=d.get("status", "open"),
                    promoted_goal_id=d.get("promoted_goal_id", ""),
                    created_at=d.get("created_at", 0.0))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class IdeaStore:
    """File-backed idea store: ideas.json under the given root."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._path = os.path.join(root, "ideas.json")
        self._ideas: dict[str, Idea] = {}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self._path):
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for d in raw.get("ideas", []):
                idea = Idea.from_dict(d)
                self._ideas[idea.id] = idea

    def _save(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ideas": [i.to_dict() for i in self._ideas.values()]},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._path)

    def capture(self, text: str, *, title: str = "") -> Idea:
        if not text.strip():
            raise ValueError("idea text is required")
        idea = Idea(id=_new_id("idea"), text=text.strip(), title=title.strip())
        self._ideas[idea.id] = idea
        self._save()
        return idea

    def get(self, idea_id: str) -> Idea:
        if idea_id not in self._ideas:
            raise KeyError(f"unknown idea: {idea_id!r}")
        return self._ideas[idea_id]

    def list(self, *, status: str = "") -> list[Idea]:
        ideas = list(self._ideas.values())
        if status:
            ideas = [i for i in ideas if i.status == status]
        return sorted(ideas, key=lambda i: i.created_at, reverse=True)

    def archive(self, idea_id: str) -> Idea:
        idea = self.get(idea_id)
        idea.status = "archived"
        self._save()
        return idea

    def promote(self, idea_id: str, goal) -> Idea:
        """Promote an idea into a Goal (domains.goals.Goal); links them."""
        idea = self.get(idea_id)
        if idea.status == "promoted":
            raise ValueError("idea already promoted")
        idea.status = "promoted"
        idea.promoted_goal_id = goal.id
        self._save()
        return idea
