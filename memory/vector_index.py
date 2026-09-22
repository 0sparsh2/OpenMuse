"""
File-backed vector index for semantic memory recall.

Production replaces this with pgvector; the interface (add / remove /
search by cosine similarity) is the seam. Vectors are stored as JSONL so the
index is inspectable and survives process restarts without a database.

Each entry: {"id", "kind", "embedding", "meta"} where meta carries the
embedding model name and any retrieval-time fields (text, source_refs).
"""
from __future__ import annotations

import json
import os
from typing import Optional

from .embeddings import cosine


class VectorIndex:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._entries: dict[str, dict] = {}
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._entries[e["id"]] = e

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for e in self._entries.values():
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)

    # -- writes ------------------------------------------------------------
    def add(self, id: str, kind: str, embedding: list[float], meta: Optional[dict] = None) -> None:
        self._entries[id] = {
            "id": id, "kind": kind, "embedding": embedding, "meta": meta or {},
        }
        self._save()

    def add_many(self, items: list[tuple[str, str, list[float], dict]]) -> None:
        for id_, kind, emb, meta in items:
            self._entries[id_] = {"id": id_, "kind": kind, "embedding": emb, "meta": meta or {}}
        self._save()

    def remove(self, ids: list[str]) -> int:
        n = 0
        for i in ids:
            if self._entries.pop(i, None) is not None:
                n += 1
        if n:
            self._save()
        return n

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, id: str) -> bool:
        return id in self._entries

    # -- search ------------------------------------------------------------
    def search(self, query_vec: list[float], top_k: int = 10,
               kinds: Optional[list[str]] = None,
               exclude_ids: Optional[set[str]] = None) -> list[dict]:
        """Cosine-similarity search. Returns [{id, kind, score, meta}]."""
        scored = []
        for e in self._entries.values():
            if kinds and e["kind"] not in kinds:
                continue
            if exclude_ids and e["id"] in exclude_ids:
                continue
            scored.append({"id": e["id"], "kind": e["kind"],
                           "score": cosine(query_vec, e["embedding"]),
                           "meta": e["meta"]})
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:top_k]
