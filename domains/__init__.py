"""Phase 9 — user-facing domain stores: Goals, Feed, Ideas.

File-backed JSON stores (same project convention as the other services).
The web client consumes them through client/serve_ui.py (/v1/local/*); the
Python client library wraps them in client/domains_client.py.
"""

from .goals import GoalStore, Goal, ActivityEntry, GoalStatus
from .feed import FeedStore, FeedItem, FeedAction
from .ideas import IdeaStore, Idea

__all__ = [
    "GoalStore", "Goal", "ActivityEntry", "GoalStatus",
    "FeedStore", "FeedItem", "FeedAction",
    "IdeaStore", "Idea",
]
