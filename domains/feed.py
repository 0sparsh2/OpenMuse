"""Feed domain: editorial cards for background results (schedules/hooks).

Each item links back to its originating run or schedule. The user can mute,
edit, or disable the source directly from the card — muting is honored here
by suppressing further items from a muted source until unmuted.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field


class FeedAction:
    VIEW_RUN = "view_run"
    MUTE_SOURCE = "mute_source"
    UNMUTE_SOURCE = "unmute_source"
    EDIT_SOURCE = "edit_source"
    DISABLE_SOURCE = "disable_source"
    DISMISS = "dismiss"


@dataclass
class FeedItem:
    id: str
    title: str
    body: str = ""
    source_type: str = ""      # schedule | hook | manual
    source_id: str = ""        # schedule/hook id the item came from
    run_id: str = ""           # originating run, when any
    url: str = ""              # optional deep link
    dismissed: bool = False
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "FeedItem":
        return FeedItem(
            id=d["id"], title=d["title"], body=d.get("body", ""),
            source_type=d.get("source_type", ""), source_id=d.get("source_id", ""),
            run_id=d.get("run_id", ""), url=d.get("url", ""),
            dismissed=bool(d.get("dismissed", False)),
            created_at=d.get("created_at", 0.0))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class FeedStore:
    """File-backed feed store: feed.json under the given root."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._path = os.path.join(root, "feed.json")
        self._items: dict[str, FeedItem] = {}
        self._muted: set[str] = set()   # muted source ids
        self._load()

    def _load(self) -> None:
        if os.path.exists(self._path):
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for d in raw.get("items", []):
                it = FeedItem.from_dict(d)
                self._items[it.id] = it
            self._muted = set(raw.get("muted_sources", []))

    def _save(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "items": [it.to_dict() for it in self._items.values()],
                "muted_sources": sorted(self._muted),
            }, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._path)

    # -- ingestion ----------------------------------------------------------
    def publish(self, *, title: str, body: str = "", source_type: str = "",
                source_id: str = "", run_id: str = "", url: str = "") -> FeedItem | None:
        """Publish an editorial card. Returns None when the source is muted."""
        if not title.strip():
            raise ValueError("feed item title is required")
        if source_id and source_id in self._muted:
            return None
        it = FeedItem(id=_new_id("feed"), title=title.strip(), body=body,
                      source_type=source_type, source_id=source_id,
                      run_id=run_id, url=url)
        self._items[it.id] = it
        self._save()
        return it

    def list(self, *, include_dismissed: bool = False) -> list[FeedItem]:
        items = [it for it in self._items.values()
                 if include_dismissed or not it.dismissed]
        return sorted(items, key=lambda it: it.created_at, reverse=True)

    def dismiss(self, item_id: str) -> FeedItem:
        it = self._get(item_id)
        it.dismissed = True
        self._save()
        return it

    def _get(self, item_id: str) -> FeedItem:
        if item_id not in self._items:
            raise KeyError(f"unknown feed item: {item_id!r}")
        return self._items[item_id]

    # -- source controls ----------------------------------------------------
    def mute_source(self, source_id: str) -> None:
        self._muted.add(source_id)
        self._save()

    def unmute_source(self, source_id: str) -> None:
        self._muted.discard(source_id)
        self._save()

    def is_muted(self, source_id: str) -> bool:
        return source_id in self._muted

    def muted_sources(self) -> list[str]:
        return sorted(self._muted)
