"""
LayeredMemory: the Phase 2 facade over all memory layers.

Layers:
  1. Working memory   — turn-scoped scratchpad (memory/working.py).
  2. Curated durable  — structured records + MEMORY.md projection (curated.py).
  3. Episodic journal — daily timestamped notes (journal.py).
  4. People/groups    — relationship pages + index (people.py).
  5. Knowledge bank   — chunked user documents with embeddings (SEAM: Phase 6
                        connectors feed documents; the vector index + recall
                        machinery is ready).
  6. Summaries        — compaction outputs (SEAM: compactor prompt ships in
                        prompts/; the run pipeline that produces summaries is
                        Phase 2+ runtime work).

One tenant per root directory: <root>/ holds curated/, journal/, people/,
vectors.jsonl, derivation.jsonl, and the MEMORY.md projection.

Recall searches curated records, journal entries, and people pages
separately, then merges with maximal marginal relevance — per the
blueprint's recall pipeline.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

from .consolidation import Consolidator, extract_candidates
from .curated import CuratedMemory
from .derivation import DerivationGraph
from .embeddings import EmbeddingProvider, default_embedder
from .forgetting import ForgettingService, ForgetResult
from .curated import _lexical_overlap, _recency_decay
from .journal import Journal
from .knowledge import KnowledgeBank, SummaryStore
from .people import PeopleIndex
from .records import MemoryCandidate, MemoryRecord, new_memory_id, utcnow
from .vector_index import VectorIndex
from .working import WorkingMemory


# Authority by source class: curated, reviewed memory outranks a raw journal
# line, which outranks a generated summary at equal relevance (blueprint).
SOURCE_AUTHORITY = {"people": 0.6, "documents": 0.5, "journal": 0.4, "summaries": 0.3}


def _blueprint_score(cos: float, query: str, text: str, source: str,
                     created_at: str = "") -> float:
    semantic = (cos + 1.0) / 2.0
    lexical = _lexical_overlap(query, text)
    recency = _recency_decay(created_at) if created_at else 0.5
    relevance = min(1.0, max(0.0, max(cos, lexical) / 0.3))  # same gate as curated recall
    return round(0.35 * semantic + 0.20 * lexical
                 + relevance * (0.15 * recency + 0.15 * SOURCE_AUTHORITY.get(source, 0.4)), 3)


class LayeredMemory:
    def __init__(self, root: str, embedder: Optional[EmbeddingProvider] = None,
                 tenant_id: str = "ten_local"):
        self.root = root
        self.tenant_id = tenant_id
        os.makedirs(self.root, exist_ok=True)
        self.embedder = embedder or default_embedder()
        self.curated = CuratedMemory(os.path.join(root, "curated"), self.embedder)
        self.journal = Journal(os.path.join(root, "journal"))
        self.people = PeopleIndex(os.path.join(root, "people"))
        self.vectors = VectorIndex(os.path.join(root, "vectors.jsonl"))
        self.derivation = DerivationGraph(os.path.join(root, "graph"))
        self.consolidator = Consolidator(self.curated)
        self.forgetting = ForgettingService(self)
        self.working = WorkingMemory()
        self.documents = KnowledgeBank(os.path.join(root, "documents"), self.embedder,
                                       self.vectors, self.derivation)
        self.summaries = SummaryStore(os.path.join(root, "summaries"), self.embedder,
                                      self.vectors, self.derivation)
        self._check_embedder()

    def _check_embedder(self) -> None:
        """Vectors from different embedding models are incomparable (and may
        differ in dimension). When the model changes, rebuild every index
        from the authoritative records."""
        marker = os.path.join(self.root, "embedder.json")
        try:
            with open(marker, encoding="utf-8") as fh:
                previous = json.load(fh).get("name")
        except (OSError, ValueError):
            previous = None if len(self.vectors) else self.embedder.name
        if previous != self.embedder.name:
            self.reindex_all()
        with open(marker, "w", encoding="utf-8") as fh:
            json.dump({"name": self.embedder.name}, fh)

    # -- write path ----------------------------------------------------------
    def remember(self, text: str, source_ref: str = "",
                 event_refs: Optional[list] = None,
                 kinds: Optional[list[str]] = None) -> dict:
        """Full write pipeline: journal -> extract -> consolidate -> index.

        Returns {"journal_entry_id", "ops": [...], "memory_ids": [...]}.
        """
        entry = self.journal.write_entry(
            title="Memory note", text=text, event_refs=event_refs or [])
        candidates = extract_candidates(text, source_ref=source_ref or entry.entry_id)
        if kinds:
            candidates = [c for c in candidates if c.kind in kinds]
        ops = self.consolidator.consolidate(candidates)
        memory_ids: list[str] = []
        for op in ops:
            if op.op in ("add", "supersede", "refine", "reinforce") and op.memory_id:
                memory_ids.append(op.memory_id)
                rec = self.curated.get(op.memory_id)
                if rec is not None:
                    self._index_record(rec)
                self.derivation.link("journal", entry.entry_id, "memory",
                                     op.memory_id, "consolidated_into")
            elif op.op == "journal_only":
                self._index_journal_entry(entry, text)
        if not memory_ids:
            # episodic-only: still make the journal entry retrievable
            self._index_journal_entry(entry, text)
        self.derivation.link("message", source_ref or entry.entry_id,
                             "journal", entry.entry_id, "extracted_from")
        return {"journal_entry_id": entry.entry_id,
                "ops": [{"op": o.op, "memory_id": o.memory_id, "detail": o.detail}
                        for o in ops],
                "memory_ids": memory_ids}

    def add_person_fact(self, name: str, fact: str, source_ref: str = "",
                        relationship: str = "") -> dict:
        person = self.people.add_fact(name, fact, source_ref, relationship)
        vec = self.embedder.embed_one(f"{name}: {fact}")
        # one vector per fact (keyed by content), so later facts don't
        # overwrite earlier ones in the index
        fid = hashlib.sha1(fact.strip().lower().encode("utf-8")).hexdigest()[:10]
        self.vectors.add(f"person:{person['person_id']}:{fid}", "person", vec,
                         {"name": person["name"], "text": fact,
                          "person_id": person["person_id"]})
        self.derivation.link("message", source_ref, "person",
                             person["person_id"], "indexed_into")
        return person

    # -- indexing --------------------------------------------------------------
    def _index_record(self, rec: MemoryRecord) -> None:
        vec = self.embedder.embed_one(rec.claim)
        self.vectors.add(f"memory:{rec.memory_id}", "memory", vec,
                         {"text": rec.claim, "kind": rec.kind,
                          "memory_id": rec.memory_id})
        rec.embedding_ref = f"memory:{rec.memory_id}"
        self.derivation.link("memory", rec.memory_id, "vector",
                             f"memory:{rec.memory_id}", "embedded_as")

    def _index_journal_entry(self, entry, text: str) -> None:
        vec = self.embedder.embed_one(f"{entry.title}: {text}")
        self.vectors.add(f"journal:{entry.entry_id}", "journal", vec,
                         {"text": text[:500], "entry_id": entry.entry_id})
        self.derivation.link("journal", entry.entry_id, "vector",
                             f"journal:{entry.entry_id}", "embedded_as")

    def reindex_all(self) -> dict:
        """Rebuild the whole vector index from authoritative records
        (recovery tool; also runs automatically when the embedder changes)."""
        self.vectors.remove(list(self.vectors._entries))
        counts = {"memory": 0, "journal": 0, "people": 0, "documents": 0, "summaries": 0}
        for rec in self.curated.all_records():
            if rec.status == "active":
                self._index_record(rec)
                counts["memory"] += 1
        for entry in self.journal.recent_entries(100_000):
            if entry.text and entry.text != "[redacted]":
                self._index_journal_entry(entry, entry.text)
                counts["journal"] += 1
        for p in self.people.list_people():
            person = self.people._people.get(p["person_id"], {})
            for f in person.get("facts", []):
                fid = hashlib.sha1(f["fact"].strip().lower().encode("utf-8")).hexdigest()[:10]
                self.vectors.add(f"person:{p['person_id']}:{fid}", "person",
                                 self.embedder.embed_one(f"{p['name']}: {f['fact']}"),
                                 {"name": p["name"], "text": f["fact"], "person_id": p["person_id"]})
                counts["people"] += 1
        if hasattr(self, "documents"):
            counts["documents"] = self.documents.reindex()
            counts["summaries"] = self.summaries.reindex()
        return counts

    # -- recall ------------------------------------------------------------------
    def recall(self, query: str, top_k: int = 5,
               sources: Optional[list[str]] = None,
               include_history: bool = False) -> list[dict]:
        """Hybrid recall across layers. Each hit: {source, memory_id/entry_id,
        text, score, source_refs, status, kind, why}."""
        sources = sources or ["curated", "journal", "people", "documents", "summaries"]
        qvec = self.embedder.embed_query(query)
        hits: list[dict] = []

        if "curated" in sources:
            for r in self.curated.recall(query, qvec, top_k=top_k,
                                         include_history=include_history):
                r["source"] = "curated"
                hits.append(r)

        if "journal" in sources:
            for s in self.vectors.search(qvec, top_k=top_k, kinds=["journal"]):
                meta = s["meta"]
                entry = self.journal.get(meta.get("entry_id", ""))
                if entry is None:
                    continue
                text = f"{entry.title}: {entry.text}"
                hits.append({
                    "source": "journal", "entry_id": entry.entry_id, "text": text,
                    "score": _blueprint_score(s["score"], query, text, "journal", entry.timestamp),
                    "cos": round(s["score"], 3),
                    "source_refs": entry.event_refs, "status": "episodic",
                    "kind": "episodic", "why": ["semantic", "episodic"],
                })

        if "people" in sources:
            for s in self.vectors.search(qvec, top_k=top_k, kinds=["person"]):
                meta = s["meta"]
                text = f"{meta.get('name', '')}: {meta.get('text', '')}"
                hits.append({
                    "source": "people", "person_id": meta.get("person_id"), "text": text,
                    "score": _blueprint_score(s["score"], query, text, "people"),
                    "cos": round(s["score"], 3),
                    "source_refs": [], "status": "active",
                    "kind": "relationship", "why": ["semantic", "relationship"],
                })

        if "documents" in sources:
            for s in self.vectors.search(qvec, top_k=top_k, kinds=["document"]):
                meta = s["meta"]
                hits.append({
                    "source": "documents", "document_id": meta.get("document_id"),
                    "text": f"[{meta.get('title', 'document')}] {meta.get('text', '')}",
                    "score": _blueprint_score(s["score"], query, meta.get("text", ""), "documents"),
                    "cos": round(s["score"], 3),
                    "source_refs": [f"{meta.get('document_id')}#chars={meta.get('char_start')}-{meta.get('char_end')}"],
                    "status": "active", "kind": "document", "why": ["semantic", "knowledge"],
                })

        if "summaries" in sources:
            for s in self.vectors.search(qvec, top_k=2, kinds=["summary"]):
                meta = s["meta"]
                hits.append({
                    "source": "summaries", "summary_id": meta.get("summary_id"),
                    "text": f"Earlier conversation summary: {meta.get('text', '')}",
                    # a generated summary never outranks raw/curated memory at equal relevance
                    "score": _blueprint_score(s["score"], query, meta.get("text", ""), "summaries"),
                    "cos": round(s["score"], 3),
                    "source_refs": [meta.get("chat_id", "")], "status": "summary",
                    "kind": "summary", "why": ["semantic", "summary"],
                })

        # merge: sort by score, dedupe identical texts, cap at top_k
        hits.sort(key=lambda h: h["score"], reverse=True)
        seen, merged = set(), []
        for h in hits:
            key = h["text"][:120].lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(h)
            if len(merged) >= top_k:
                break
        return merged

    # -- forgetting ----------------------------------------------------------------
    def forget(self, query: str, mode: str = "tombstone") -> ForgetResult:
        return self.forgetting.forget(query, mode)

    # -- projections -----------------------------------------------------------------
    def memory_md(self) -> str:
        """Path to the regenerated MEMORY.md projection."""
        return os.path.join(self.curated.root, "MEMORY.md")

    def stats(self) -> dict:
        return {
            "curated_active": len(self.curated.active_records()),
            "curated_total": len(self.curated.all_records()),
            "journal_entries": len(self.journal.recent_entries(10_000)),
            "people": len(self.people.list_people()),
            "documents": len(self.documents.list()),
            "summaries": len(self.summaries.list()),
            "vectors": len(self.vectors),
            "embedder": self.embedder.name,
        }
