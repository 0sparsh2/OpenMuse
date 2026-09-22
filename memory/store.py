"""
Phase 1 memory store — the seam for the Phase 2 layered memory system.

Phase 1 contract: `append_note(text) -> ref`, `recent_notes(limit) -> list`.
Phase 2 replaces this module with the layered architecture (curated durable
memory, episodic journal, knowledge bank, people/groups, forgetting) while
keeping the tool contract (`memory.note`) unchanged.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone


class MemoryStore:
    """File-backed note store. One tenant per root directory."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(self.root, exist_ok=True)
        self._notes_path = os.path.join(self.root, "notes.md")

    def append_note(self, text: str) -> dict:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(self._notes_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n## {stamp}\n{text.strip()}\n")
        return {"stored": True, "ref": "notes.md"}

    def recent_notes(self, limit: int = 20) -> list[str]:
        if not os.path.exists(self._notes_path):
            return []
        with open(self._notes_path, "r", encoding="utf-8") as fh:
            chunks = [c.strip() for c in fh.read().split("\n## ") if c.strip()]
        return chunks[-limit:]
