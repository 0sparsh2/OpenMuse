"""Phase 9 — client wrappers for the Goals / Feed / Ideas domain stores.

The web UI consumes the same stores over HTTP (/v1/local/*) via
client/serve_ui.py; this module is the Python client-library equivalent.
"""
from __future__ import annotations

import os

from domains import GoalStore, FeedStore, IdeaStore, FeedAction  # noqa: F401


class GoalsClient:
    """Goals tab: CRUD, activity entries, attachments, status tracking."""

    def __init__(self, root: str):
        self.store = GoalStore(os.path.join(root, "domains"))

    def create(self, **kw) -> dict:
        return self.store.create(**kw).to_dict()

    def get(self, goal_id: str) -> dict:
        return self.store.get(goal_id).to_dict()

    def list(self, *, status: str = "") -> list[dict]:
        return [g.to_dict() for g in self.store.list(status=status)]

    def update(self, goal_id: str, **fields) -> dict:
        return self.store.update(goal_id, **fields).to_dict()

    def set_status(self, goal_id: str, status: str) -> dict:
        return self.store.set_status(goal_id, status).to_dict()

    def delete(self, goal_id: str) -> None:
        self.store.delete(goal_id)

    def log_activity(self, goal_id: str, text: str, *,
                     source: str = "manual", source_run_id: str = "") -> dict:
        from dataclasses import asdict
        return asdict(self.store.log_activity(
            goal_id, text, source=source, source_run_id=source_run_id))

    def attach(self, goal_id: str, artifact_id: str) -> dict:
        return self.store.attach(goal_id, artifact_id).to_dict()


class FeedClient:
    """Feed tab: editorial cards with source controls."""

    def __init__(self, root: str):
        self.store = FeedStore(os.path.join(root, "domains"))

    def publish(self, **kw):
        item = self.store.publish(**kw)
        return item.to_dict() if item else None

    def list(self, *, include_dismissed: bool = False) -> list[dict]:
        return [i.to_dict() for i in self.store.list(
            include_dismissed=include_dismissed)]

    def dismiss(self, item_id: str) -> dict:
        return self.store.dismiss(item_id).to_dict()

    def mute_source(self, source_id: str) -> None:
        self.store.mute_source(source_id)

    def unmute_source(self, source_id: str) -> None:
        self.store.unmute_source(source_id)

    def muted_sources(self) -> list[str]:
        return self.store.muted_sources()


class IdeasClient:
    """Ideas tab: capture, list, promote into goals."""

    def __init__(self, root: str, goals_client: GoalsClient | None = None):
        self.store = IdeaStore(os.path.join(root, "domains"))
        self.goals = goals_client

    def capture(self, text: str, *, title: str = "") -> dict:
        return self.store.capture(text, title=title).to_dict()

    def list(self, *, status: str = "") -> list[dict]:
        return [i.to_dict() for i in self.store.list(status=status)]

    def archive(self, idea_id: str) -> dict:
        return self.store.archive(idea_id).to_dict()

    def promote(self, idea_id: str, *, goal_title: str = "",
                goal_description: str = "") -> dict:
        """Promote an idea into a new goal; links them both ways."""
        if self.goals is None:
            raise RuntimeError("no goals client bound")
        idea = self.store.get(idea_id)
        goal = self.goals.store.create(
            title=goal_title or idea.title or idea.text[:60],
            description=goal_description or idea.text,
            category="from-idea")
        self.goals.store.log_activity(
            goal.id, f"Promoted from idea {idea.id}: {idea.text[:120]}",
            source="manual")
        promoted = self.store.promote(idea_id, goal)
        return {"idea": promoted.to_dict(), "goal": goal.to_dict()}
