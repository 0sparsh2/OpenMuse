"""
Derivation graph: the forget graph from the blueprint.

Edges: message -> memory candidate -> durable memory -> MEMORY.md line;
message -> chat summary -> goal summary; message -> knowledge chunk ->
embedding; person fact -> relationship page -> relationship index.

The forgetting workflow walks this graph to find every derivative of a
target record so nothing resurfaces through recall, summaries, or indexes.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict, deque

from .records import DerivationEdge


class DerivationGraph:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(self.root, exist_ok=True)
        self._path = os.path.join(root, "derivation.jsonl")
        self._edges: list[DerivationEdge] = []
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        with open(self._path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._edges.append(DerivationEdge.from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue

    def _save(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for e in self._edges:
                fh.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")
        os.replace(tmp, self._path)

    def link(self, from_kind: str, from_id: str, to_kind: str, to_id: str,
             relation: str) -> DerivationEdge:
        edge = DerivationEdge(from_kind, from_id, to_kind, to_id, relation)
        self._edges.append(edge)
        self._save()
        return edge

    def derivatives_of(self, kind: str, id: str) -> list[DerivationEdge]:
        """Transitive closure of everything derived from (kind, id)."""
        out: list[DerivationEdge] = []
        seen: set[tuple] = set()
        queue: deque[tuple[str, str]] = deque([(kind, id)])
        by_source: dict[tuple, list[DerivationEdge]] = defaultdict(list)
        for e in self._edges:
            by_source[(e.from_kind, e.from_id)].append(e)
        while queue:
            node = queue.popleft()
            for e in by_source.get(node, []):
                key = (e.to_kind, e.to_id, e.relation)
                if key in seen:
                    continue
                seen.add(key)
                out.append(e)
                queue.append((e.to_kind, e.to_id))
        return out

    def sources_of(self, kind: str, id: str) -> list[DerivationEdge]:
        return [e for e in self._edges if e.to_kind == kind and e.to_id == id]

    def remove_node(self, kind: str, id: str) -> int:
        """Drop all edges touching (kind, id). Returns edges removed."""
        before = len(self._edges)
        self._edges = [e for e in self._edges
                       if not ((e.from_kind, e.from_id) == (kind, id)
                               or (e.to_kind, e.to_id) == (kind, id))]
        removed = before - len(self._edges)
        if removed:
            self._save()
        return removed
