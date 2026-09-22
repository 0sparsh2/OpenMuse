"""
Phase 2 memory store — layered memory behind the Phase 1 interface.

Contract preserved: `MemoryStore(root)` with `append_note(text) -> ref` and
`recent_notes(limit) -> list`. The implementation now routes through the
layered system: every note lands in the episodic journal, and explicit
durable statements are extracted and consolidated into curated memory with
embeddings, derivation edges, and a regenerated MEMORY.md projection.

Direct access to the full system is available via `.layered`.
"""
from __future__ import annotations

import os

from .layered import LayeredMemory


class MemoryStore:
    """File-backed memory store. One tenant per root directory."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(self.root, exist_ok=True)
        self.layered = LayeredMemory(os.path.join(root, "layered"))
        # Phase 1 legacy path: kept for backward compatibility of the
        # notes.md file; new writes go through the layered system.
        self._notes_path = os.path.join(self.root, "notes.md")

    def append_note(self, text: str, source_ref: str = "") -> dict:
        """Journal the note; extract and consolidate durable statements."""
        result = self.layered.remember(text, source_ref=source_ref)
        entry = self.layered.journal.get(result["journal_entry_id"])
        day_path = os.path.join(
            self.layered.journal.root, entry.timestamp[:10] + ".md") if entry else ""
        return {
            "stored": True,
            "ref": result["journal_entry_id"],
            "path": os.path.relpath(day_path, self.root) if day_path else "",
            "memory_ids": result["memory_ids"],
            "ops": result["ops"],
        }

    def recent_notes(self, limit: int = 20) -> list[str]:
        entries = self.layered.journal.recent_entries(limit)
        return [f"[{e.timestamp}] {e.title}: {e.text}" for e in entries]

    # -- Phase 2 conveniences ------------------------------------------------
    def recall(self, query: str, top_k: int = 5) -> list[dict]:
        return self.layered.recall(query, top_k=top_k)

    def forget(self, query: str, mode: str = "tombstone"):
        return self.layered.forget(query, mode)

    def stats(self) -> dict:
        return self.layered.stats()
