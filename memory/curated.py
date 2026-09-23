"""
Curated durable memory (Layer 2): structured records with Markdown projection.

The authoritative store is <root>/records.jsonl (one MemoryRecord per line).
MEMORY.md is a regenerated projection — grouped Facts / Preferences /
Commitments with [mem:...] citations — never edited by hand by the runtime
(user edits go through an import job; here: `apply_markdown_edits` is a
documented seam).

Recall is the blueprint's retrieval pipeline:
  1. Query planning is the caller's job (see recall planner prompt); this
     module executes one planned query across curated records.
  2. Hybrid ranking:
       score = 0.35 * semantic + 0.20 * lexical + 0.15 * recency
             + 0.15 * authority + 0.10 * commitment_bonus + 0.05 * user_confirmed
             - duplicate_penalty - contradiction_penalty
  3. Maximal marginal relevance diversifies the final list.
  4. Superseded records are excluded unless include_history=True.
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from typing import Optional

from .embeddings import EmbeddingProvider, cosine
from .records import MemoryRecord, utcnow

_WORD_RE = re.compile(r"[a-z0-9]+")

SECTION_TITLES = {
    "stable_fact": "Facts",
    "preference": "Preferences",
    "commitment": "Commitments",
    "relationship_update": "Relationships",
    "operating_lesson": "Operating lessons",
}


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


def _lexical_overlap(query: str, text: str) -> float:
    q, t = _tokens(query), _tokens(text)
    if not q:
        return 0.0
    return len(q & t) / len(q)


def _recency_decay(created_at: str, half_life_days: float = 180.0) -> float:
    try:
        dt = datetime.fromisoformat(created_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return 0.5
    if age_days < 0:
        age_days = 0.0
    return math.exp(-age_days / half_life_days)


class CuratedMemory:
    def __init__(self, root: str, embedder: EmbeddingProvider):
        self.root = root
        self.embedder = embedder
        os.makedirs(self.root, exist_ok=True)
        self._records_path = os.path.join(self.root, "records.jsonl")
        self._projection_path = os.path.join(self.root, "MEMORY.md")
        self._records: dict[str, MemoryRecord] = {}
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not os.path.exists(self._records_path):
            return
        with open(self._records_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = MemoryRecord.from_dict(json.loads(line))
                except (json.JSONDecodeError, TypeError):
                    continue
                self._records[r.memory_id] = r

    def _save(self) -> None:
        tmp = self._records_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in self._records.values():
                fh.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")
        os.replace(tmp, self._records_path)

    # -- writes ------------------------------------------------------------
    def add(self, record: MemoryRecord) -> MemoryRecord:
        self._records[record.memory_id] = record
        self._save()
        self.regenerate_projection()
        return record

    def update(self, record: MemoryRecord) -> MemoryRecord:
        if record.memory_id not in self._records:
            raise KeyError(f"unknown memory {record.memory_id}")
        self._records[record.memory_id] = record
        self._save()
        self.regenerate_projection()
        return record

    def get(self, memory_id: str) -> Optional[MemoryRecord]:
        return self._records.get(memory_id)

    def active_records(self) -> list[MemoryRecord]:
        return [r for r in self._records.values() if r.status == "active"]

    def all_records(self) -> list[MemoryRecord]:
        return list(self._records.values())

    def find_by_predicate(self, predicate: str, kind: str = "",
                          include_superseded: bool = False) -> list[MemoryRecord]:
        out = []
        for r in self._records.values():
            if r.predicate != predicate:
                continue
            if kind and r.kind != kind:
                continue
            if not include_superseded and r.status != "active":
                continue
            out.append(r)
        return out

    # -- Markdown projection -----------------------------------------------
    def regenerate_projection(self) -> str:
        """Rebuild MEMORY.md from active structured records. Returns the path."""
        sections: dict[str, list[MemoryRecord]] = {}
        for r in self.active_records():
            sections.setdefault(SECTION_TITLES.get(r.kind, "Facts"), []).append(r)
        lines = ["# Memory", "",
                 "_Curated durable memory. Regenerated from structured records; "
                 "edit via the memory tools so provenance is preserved._", ""]
        for title in ["Facts", "Preferences", "Commitments", "Relationships", "Operating lessons"]:
            recs = sections.get(title, [])
            if not recs:
                continue
            lines.append(f"## {title}")
            for r in sorted(recs, key=lambda x: x.created_at):
                lines.append(f"- {r.claim} [{r.memory_id}]")
            lines.append("")
        with open(self._projection_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines).rstrip() + "\n")
        return self._projection_path

    # -- recall ------------------------------------------------------------
    def recall(self, query: str, query_vec: list[float], top_k: int = 5,
               kinds: Optional[list[str]] = None,
               include_history: bool = False) -> list[dict]:
        """Hybrid-ranked recall over curated records. Returns result dicts
        with memory_id, text, score, source_refs, status, and why-tags."""
        candidates = [r for r in self._records.values()
                      if (include_history or r.status == "active")
                      and (not kinds or r.kind in kinds)]
        if not candidates:
            return []

        rec_vecs = self.embedder.embed([r.claim for r in candidates])
        scored = []
        for r, rvec in zip(candidates, rec_vecs):
            semantic = (cosine(query_vec, rvec) + 1.0) / 2.0  # -> [0,1]
            lexical = _lexical_overlap(query, r.claim)
            recency = _recency_decay(r.created_at)
            authority = 1.0 if r.reviewed_by_user else (0.7 if r.confidence >= 0.9 else 0.5)
            commitment_bonus = 1.0 if (r.kind == "commitment" and r.valid_to is None) else 0.0
            user_confirmed = 1.0 if r.reviewed_by_user else 0.0
            # Priors (recency, authority, commitment, confirmation) break ties
            # between RELEVANT memories; gated by relevance so an unrelated
            # but recent/authoritative record can't outrank a real match.
            relevance = min(1.0, max(0.0, max(semantic * 2 - 1, lexical) / 0.3))
            score = (0.35 * semantic + 0.20 * lexical
                     + relevance * (0.15 * recency + 0.15 * authority
                                    + 0.10 * commitment_bonus + 0.05 * user_confirmed))
            why = []
            if semantic > 0.6:
                why.append("semantic")
            if lexical > 0.3:
                why.append("lexical")
            if r.kind == "commitment":
                why.append("commitment")
            if r.reviewed_by_user:
                why.append("user_confirmed")
            why.append("current" if r.status == "active" else r.status)
            scored.append({"record": r, "score": score, "why": why,
                           "vec": rvec, "semantic": semantic})

        # duplicate penalty: same predicate+normalized value already ranked higher
        scored.sort(key=lambda s: s["score"], reverse=True)
        seen: set[tuple] = set()
        for s in scored:
            r = s["record"]
            key = (r.predicate, json.dumps(r.value, sort_keys=True))
            if key in seen and r.predicate:
                s["score"] -= 0.25
                s["why"].append("duplicate_penalty")
            else:
                seen.add(key)

        # MMR diversification
        selected = self._mmr(scored, top_k, lambda_=0.7)

        results = []
        for s in selected:
            r = s["record"]
            results.append({
                "memory_id": r.memory_id, "text": r.claim,
                "score": round(max(0.0, s["score"]), 3),
                "source_refs": r.source_refs, "status": r.status,
                "kind": r.kind, "why": s["why"],
                "cos": round(s["semantic"] * 2 - 1, 3),  # raw similarity, for relevance floors
            })
        return results

    @staticmethod
    def _mmr(scored: list[dict], top_k: int, lambda_: float = 0.7) -> list[dict]:
        selected: list[dict] = []
        remaining = sorted(scored, key=lambda s: s["score"], reverse=True)
        while remaining and len(selected) < top_k:
            best, best_val = None, float("-inf")
            for s in remaining:
                div = max([cosine(s["vec"], o["vec"]) for o in selected] or [0.0])
                val = lambda_ * s["score"] - (1 - lambda_) * div
                if val > best_val:
                    best, best_val = s, val
            selected.append(best)
            remaining.remove(best)
        return selected
