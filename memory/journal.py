"""
Episodic journal (Layer 3): timestamped daily notes capturing what happened.

Daily files under <root>/journal/YYYY-MM-DD.md are the human-readable
projection; a JSONL sidecar (<root>/journal/entries.jsonl) is the structured
record with embedding refs for semantic recall over episodic memory.

Journal entries are append-only except for corrections and redactions. They
record events, not necessarily durable truths — durable facts live in the
curated layer. Each entry links to event IDs when available.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

from .records import JournalEntry, new_id, utcnow


class Journal:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(self.root, exist_ok=True)
        self._index_path = os.path.join(self.root, "entries.jsonl")

    # -- writes ------------------------------------------------------------
    def write_entry(self, title: str, text: str,
                    event_refs: Optional[list] = None,
                    tags: Optional[list] = None,
                    timestamp: Optional[str] = None) -> JournalEntry:
        entry = JournalEntry(
            entry_id=new_id("evt"),
            timestamp=timestamp or utcnow(),
            title=title.strip(),
            text=text.strip(),
            event_refs=event_refs or [],
            tags=tags or [],
        )
        self._append_projection(entry)
        self._append_index(entry)
        return entry

    def redact_entry(self, entry_id: str, replacement: str = "[redacted]") -> bool:
        """Correction/redaction path: rewrites the entry text in both stores."""
        entries = self._read_index()
        found = False
        for e in entries:
            if e["entry_id"] == entry_id:
                e["text"] = replacement
                found = True
        if not found:
            return False
        self._rewrite_index(entries)
        self._rewrite_projection(entries)
        return True

    # -- reads -------------------------------------------------------------
    def recent_entries(self, limit: int = 20) -> list[JournalEntry]:
        entries = self._read_index()
        return [JournalEntry.from_dict(e) for e in entries[-limit:]]

    def entries_for_date(self, date: str) -> list[JournalEntry]:
        """date: YYYY-MM-DD (UTC)."""
        return [e for e in self.recent_entries(10_000) if e.timestamp.startswith(date)]

    def get(self, entry_id: str) -> Optional[JournalEntry]:
        for e in self._read_index():
            if e["entry_id"] == entry_id:
                return JournalEntry.from_dict(e)
        return None

    # -- projection (Markdown) ---------------------------------------------
    def _day_path(self, timestamp: str) -> str:
        day = timestamp[:10]
        return os.path.join(self.root, f"{day}.md")

    def _append_projection(self, entry: JournalEntry) -> None:
        path = self._day_path(entry.timestamp)
        is_new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as fh:
            if is_new:
                fh.write(f"# {entry.timestamp[:10]}\n")
            time = entry.timestamp[11:16] if len(entry.timestamp) >= 16 else entry.timestamp
            fh.write(f"\n## {time} — {entry.title} [{entry.entry_id}]\n{entry.text}\n")

    def _rewrite_projection(self, entries: list[dict]) -> None:
        by_day: dict[str, list[dict]] = {}
        for e in entries:
            by_day.setdefault(e["timestamp"][:10], []).append(e)
        for day, day_entries in by_day.items():
            with open(os.path.join(self.root, f"{day}.md"), "w", encoding="utf-8") as fh:
                fh.write(f"# {day}\n")
                for e in day_entries:
                    time = e["timestamp"][11:16] if len(e["timestamp"]) >= 16 else e["timestamp"]
                    fh.write(f"\n## {time} — {e['title']} [{e['entry_id']}]\n{e['text']}\n")

    # -- structured index ----------------------------------------------------
    def _read_index(self) -> list[dict]:
        if not os.path.exists(self._index_path):
            return []
        out = []
        with open(self._index_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return out

    def _append_index(self, entry: JournalEntry) -> None:
        with open(self._index_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

    def _rewrite_index(self, entries: list[dict]) -> None:
        tmp = self._index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, self._index_path)

    def set_embedding_ref(self, entry_id: str, ref: str) -> None:
        entries = self._read_index()
        for e in entries:
            if e["entry_id"] == entry_id:
                e["embedding_ref"] = ref
        self._rewrite_index(entries)
