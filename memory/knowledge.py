"""
Knowledge bank (Layer 5) and conversation summaries (Layer 6).

KnowledgeBank: user documents chunked on paragraph boundaries (~900 chars,
150 overlap), each chunk embedded as a passage and indexed with its source
offsets, so recall can cite "document X, chars 810-1712". Raw text lives in
<root>/documents/<doc_id>.txt; the index row carries only references.

SummaryStore: one rolling structured summary per chat (the blueprint's
summary schema). Summaries are navigation aids pointing at source turns; they
never overwrite raw messages and never write durable memory themselves.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

from .records import new_id, utcnow

CHUNK_CHARS = 900
CHUNK_OVERLAP = 150


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[tuple[int, int]]:
    """Return (start, end) spans, preferring paragraph/sentence boundaries."""
    spans, start, n = [], 0, len(text)
    while start < n:
        end = min(n, start + size)
        if end < n:
            window = text[start:end]
            cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("\n"))
            if cut > size * 0.5:
                end = start + cut + 1
        spans.append((start, end))
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return spans


class KnowledgeBank:
    def __init__(self, root: str, embedder, vectors, derivation):
        self.root = root
        self.embedder = embedder
        self.vectors = vectors
        self.derivation = derivation
        os.makedirs(root, exist_ok=True)
        self._index_path = os.path.join(root, "index.json")
        self._docs: dict[str, dict] = self._read()

    def _read(self) -> dict:
        try:
            with open(self._index_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        tmp = self._index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._docs, fh, indent=1)
        os.replace(tmp, self._index_path)

    def add(self, title: str, text: str, source_type: str = "user_file") -> dict:
        text = (text or "").replace("\r\n", "\n").strip()
        if not text:
            raise ValueError("document is empty")
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        for d in self._docs.values():
            if d["content_sha256"] == sha:
                return d  # identical content already indexed
        doc_id = new_id("doc")
        with open(os.path.join(self.root, doc_id + ".txt"), "w", encoding="utf-8") as fh:
            fh.write(text)
        spans = chunk_text(text)
        vecs = self.embedder.embed([text[a:b] for a, b in spans])
        items, chunk_ids = [], []
        for i, ((a, b), vec) in enumerate(zip(spans, vecs)):
            cid = f"doc:{doc_id}:{i}"
            chunk_ids.append(cid)
            items.append((cid, "document", vec, {
                "document_id": doc_id, "title": title, "ordinal": i,
                "char_start": a, "char_end": b, "text": text[a:b],
                "embedding_model": self.embedder.name}))
            self.derivation.link("document", doc_id, "vector", cid, "chunked_into")
        self.vectors.add_many(items)
        doc = {"document_id": doc_id, "title": title or "Untitled", "source_type": source_type,
               "chars": len(text), "chunks": len(spans), "content_sha256": sha,
               "created_at": utcnow()}
        self._docs[doc_id] = doc
        self._save()
        return doc

    def list(self) -> list[dict]:
        return sorted(self._docs.values(), key=lambda d: d["created_at"], reverse=True)

    def get_text(self, doc_id: str) -> Optional[str]:
        path = os.path.join(self.root, doc_id + ".txt")
        if doc_id not in self._docs or not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def delete(self, doc_id: str) -> bool:
        doc = self._docs.pop(doc_id, None)
        if doc is None:
            return False
        self.vectors.remove([f"doc:{doc_id}:{i}" for i in range(doc["chunks"])])
        path = os.path.join(self.root, doc_id + ".txt")
        if os.path.exists(path):
            os.remove(path)
        self.derivation.remove_node("document", doc_id)
        self._save()
        return True

    def reindex(self) -> int:
        n = 0
        for doc_id, doc in self._docs.items():
            text = self.get_text(doc_id) or ""
            spans = [(a, b) for a, b in chunk_text(text)]
            vecs = self.embedder.embed([text[a:b] for a, b in spans]) if spans else []
            self.vectors.add_many([
                (f"doc:{doc_id}:{i}", "document", v,
                 {"document_id": doc_id, "title": doc["title"], "ordinal": i,
                  "char_start": a, "char_end": b, "text": text[a:b],
                  "embedding_model": self.embedder.name})
                for i, ((a, b), v) in enumerate(zip(spans, vecs))])
            n += len(spans)
        return n


SUMMARY_FIELDS = ("decisions", "commitments", "stable_facts", "open_threads",
                  "safety_relevant", "artifacts", "omissions")


class SummaryStore:
    def __init__(self, root: str, embedder, vectors, derivation):
        self.root = root
        self.embedder = embedder
        self.vectors = vectors
        self.derivation = derivation
        os.makedirs(root, exist_ok=True)

    def _path(self, chat_id: str) -> str:
        safe = "".join(c for c in chat_id if c.isalnum() or c in "_-")
        return os.path.join(self.root, safe + ".json")

    def get(self, chat_id: str) -> Optional[dict]:
        try:
            with open(self._path(chat_id), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def put(self, chat_id: str, summary: dict, source_refs: list[str]) -> dict:
        prev = self.get(chat_id)
        rec = {"summary_id": prev["summary_id"] if prev else new_id("sum"),
               "chat_id": chat_id,
               "from_sequence": summary.get("from_sequence", 0),
               "to_sequence": summary["to_sequence"],
               "source_refs": source_refs,
               "prompt_version": summary.get("prompt_version", "compactor@1.0.0"),
               "created_at": utcnow()}
        for f in SUMMARY_FIELDS:
            rec[f] = [str(x)[:400] for x in (summary.get(f) or [])][:20]
        rec["text"] = str(summary.get("text") or "")[:2000]
        tmp = self._path(chat_id) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=1)
        os.replace(tmp, self._path(chat_id))
        body = self.render(rec)
        self.vectors.add(f"summary:{chat_id}", "summary", self.embedder.embed([body])[0],
                         {"chat_id": chat_id, "summary_id": rec["summary_id"], "text": body[:1500],
                          "embedding_model": self.embedder.name})
        for ref in source_refs:
            self.derivation.link("message", ref, "summary", rec["summary_id"], "summarized_into")
        return rec

    def list(self) -> list[dict]:
        out = []
        for fn in os.listdir(self.root):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(self.root, fn), encoding="utf-8") as fh:
                        out.append(json.load(fh))
                except (OSError, ValueError):
                    continue
        return sorted(out, key=lambda r: r.get("created_at", ""), reverse=True)

    @staticmethod
    def render(rec: dict) -> str:
        lines = []
        if rec.get("text"):
            lines.append(rec["text"])
        labels = {"decisions": "Decisions", "commitments": "Commitments",
                  "stable_facts": "Facts the user stated", "open_threads": "Open threads",
                  "safety_relevant": "Authorization/safety notes", "artifacts": "Artifacts & identifiers"}
        for f, label in labels.items():
            if rec.get(f):
                lines.append(f"{label}: " + "; ".join(rec[f]))
        return "\n".join(lines)

    def reindex(self) -> int:
        n = 0
        for rec in self.list():
            body = self.render(rec)
            self.vectors.add(f"summary:{rec['chat_id']}", "summary", self.embedder.embed([body])[0],
                             {"chat_id": rec["chat_id"], "summary_id": rec["summary_id"],
                              "text": body[:1500], "embedding_model": self.embedder.name})
            n += 1
        return n
