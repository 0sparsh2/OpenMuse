"""
User-initiated forgetting: a first-class workflow, not an embedding deletion.

Pipeline (per blueprint "User-initiated forgetting"):
  1. Resolve the target conservatively. Zero matches -> NOT_FOUND; more than
     one plausible match -> AMBIGUOUS with safe labels (record IDs and kinds,
     never secret or sensitive values). Never broaden the target.
  2. Plan: walk the derivation graph for affected source records and
     derivatives (durable records, vectors, journal entries, people pages,
     projection lines).
  3. Execute: delete or tombstone durable records (tombstone is the default:
     content is dropped but a non-content audit marker remains), remove
     vectors and derived chunks, regenerate Markdown projections and the
     people index, invalidate the derivation edges.
  4. Audit: append a minimal non-content marker ("user-directed deletion
     completed") — the forgotten value is never retained.
  5. Verify: lexical + semantic search no longer return the item.

The runtime performs the writes; the planner only proposes. `ForgetPlanner`
is the deterministic planner (the LLM "forgetting planner prompt" lives in
prompts/forget-planner.md for the ambiguous/complex cases).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from .curated import _lexical_overlap
from .derivation import DerivationGraph
from .records import utcnow


@dataclass
class ForgetPlan:
    status: str  # ready | ambiguous | not_found
    targets: list[dict] = field(default_factory=list)  # [{kind, id, label}]
    derivatives: list[dict] = field(default_factory=list)
    mode: str = "tombstone"  # tombstone | delete
    note: str = ""


@dataclass
class ForgetResult:
    status: str  # completed | ambiguous | not_found | failed
    removed: list[dict] = field(default_factory=list)
    verified: bool = False
    note: str = ""


class ForgettingService:
    """Executes the forgetting workflow against a LayeredMemory."""

    def __init__(self, layered: "LayeredMemory"):
        self.m = layered

    # -- 1+2. resolve + plan --------------------------------------------------
    def plan(self, query: str, mode: str = "tombstone") -> ForgetPlan:
        matches = self._resolve(query)
        if not matches:
            return ForgetPlan(status="not_found",
                              note=f"no memory matches {query!r}")
        if len(matches) > 1:
            return ForgetPlan(
                status="ambiguous",
                targets=[{"kind": t["kind"], "id": t["id"], "label": t["label"]}
                         for t in matches],
                mode=mode,
                note="multiple records match; specify one by ID")
        target = matches[0]
        derivatives = self._derivatives(target)
        return ForgetPlan(status="ready", targets=[target],
                          derivatives=derivatives, mode=mode)

    def _resolve(self, query: str) -> list[dict]:
        """Conservative target resolution across curated, journal, people."""
        hits: list[dict] = []
        ql = query.lower()
        for r in self.m.curated.all_records():
            if r.status not in ("active", "superseded"):
                continue
            if ql in r.claim.lower() or _lexical_overlap(query, r.claim) >= 0.5:
                hits.append({"kind": "memory", "id": r.memory_id,
                             "label": f"{r.kind} [{r.memory_id}]",
                             "score": _lexical_overlap(query, r.claim)})
        for e in self.m.journal.recent_entries(10_000):
            if ql in (e.title + " " + e.text).lower():
                hits.append({"kind": "journal", "id": e.entry_id,
                             "label": f"journal entry [{e.entry_id}]",
                             "score": 0.4})
        for p in self.m.people.list_people():
            if ql in p["name"].lower():
                hits.append({"kind": "person", "id": p["person_id"],
                             "label": f"person '{p['name']}' [{p['person_id']}]",
                             "score": 0.6})
        # collapse to one hit per id, keep best score
        best: dict[str, dict] = {}
        for h in hits:
            key = (h["kind"], h["id"])
            if key not in best or h["score"] > best[key]["score"]:
                best[key] = h
        targets = list(best.values())
        # A journal entry that sourced a matched memory/person record is not a
        # competing target — it is a source derivative handled in the plan.
        memory_ids = {h["id"] for h in targets if h["kind"] == "memory"}
        person_ids = {h["id"] for h in targets if h["kind"] == "person"}
        sourced_journal: set[str] = set()
        for mid in memory_ids:
            for e in self.m.derivation.sources_of("memory", mid):
                if e.relation in ("consolidated_into", "extracted_from"):
                    sourced_journal.add(e.from_id)
        for pid in person_ids:
            for e in self.m.derivation.sources_of("person", pid):
                if e.relation in ("indexed_into", "extracted_from"):
                    sourced_journal.add(e.from_id)
        targets = [h for h in targets
                   if not (h["kind"] == "journal" and h["id"] in sourced_journal
                           and (memory_ids or person_ids))]
        # ambiguous only when 2+ *distinct* targets with real overlap
        return sorted(targets, key=lambda h: h["score"], reverse=True)

    def _derivatives(self, target: dict) -> list[dict]:
        kind, tid = target["kind"], target["id"]
        edges = self.m.derivation.derivatives_of(kind, tid)
        out = [{"kind": e.to_kind, "id": e.to_id, "relation": e.relation}
               for e in edges]
        # source records are affected too (e.g. the journal entry a durable
        # memory was consolidated from) — they get redacted on execute
        for e in self.m.derivation.sources_of(kind, tid):
            if e.relation in ("consolidated_into", "extracted_from", "indexed_into"):
                out.append({"kind": e.from_kind, "id": e.from_id,
                            "relation": "source_of"})
        return out

    # -- 3+4. execute ----------------------------------------------------------
    def execute(self, plan: ForgetPlan) -> ForgetResult:
        if plan.status != "ready":
            return ForgetResult(status=plan.status, note=plan.note)
        removed: list[dict] = []
        for t in plan.targets:
            removed.extend(self._remove_target(t, plan.mode))
        for d in plan.derivatives:
            removed.extend(self._remove_derivative(d, plan.mode))
        # regenerate every projection so derivatives vanish from views
        self.m.curated.regenerate_projection()
        self._audit(plan)
        return ForgetResult(status="completed", removed=removed,
                            verified=self._verify(plan))

    def _remove_target(self, target: dict, mode: str) -> list[dict]:
        kind, tid = target["kind"], target["id"]
        removed: list[dict] = []
        if kind == "memory":
            rec = self.m.curated.get(tid)
            if rec is None:
                return removed
            if mode == "delete":
                # full delete: drop the record row entirely
                self.m.curated._records.pop(tid, None)
                self.m.curated._save()
            else:
                rec.status = "tombstoned"
                rec.claim = "[tombstoned by user-directed forgetting]"
                rec.value = {}
                rec.predicate = ""
                self.m.curated.update(rec)
            removed.append({"kind": "memory", "id": tid, "how": mode})
        elif kind == "journal":
            if self.m.journal.redact_entry(tid):
                removed.append({"kind": "journal", "id": tid, "how": "redacted"})
        elif kind == "person":
            if self.m.people.remove_person(tid):
                removed.append({"kind": "person", "id": tid, "how": "deleted"})
        # vectors + derivation edges always go, regardless of mode
        self.m.vectors.remove([f"memory:{tid}", f"journal:{tid}", f"person:{tid}"])
        self.m.derivation.remove_node(kind, tid)
        return removed

    def _remove_derivative(self, deriv: dict, mode: str) -> list[dict]:
        kind, tid = deriv["kind"], deriv["id"]
        removed: list[dict] = []
        if kind == "vector":
            n = self.m.vectors.remove([tid])
            if n:
                removed.append({"kind": "vector", "id": tid, "how": "deleted"})
        elif kind == "journal":
            if self.m.journal.redact_entry(tid):
                removed.append({"kind": "journal", "id": tid, "how": "redacted"})
        elif kind == "person":
            if self.m.people.remove_person(tid):
                removed.append({"kind": "person", "id": tid, "how": "deleted"})
        elif kind == "memory" and mode == "delete":
            rec = self.m.curated.get(tid)
            if rec is not None and rec.status != "active":
                self.m.curated._records.pop(tid, None)
                self.m.curated._save()
                removed.append({"kind": "memory", "id": tid, "how": "deleted"})
        self.m.derivation.remove_node(kind, tid)
        return removed

    def _audit(self, plan: ForgetPlan) -> None:
        path = os.path.join(self.m.root, "forget-audit.log")
        ids = ",".join(t["id"] for t in plan.targets)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{utcnow()} user-directed deletion completed "
                     f"mode={plan.mode} targets={ids}\n")

    # -- 5. verify --------------------------------------------------------------
    def _verify(self, plan: ForgetPlan) -> bool:
        """Semantic + lexical search must no longer surface the targets."""
        target_ids = {t["id"] for t in plan.targets}
        # lexical: projection + records
        proj_path = os.path.join(self.m.curated.root, "MEMORY.md")
        if os.path.exists(proj_path):
            with open(proj_path, "r", encoding="utf-8") as fh:
                proj = fh.read()
            for tid in target_ids:
                if tid in proj:
                    return False
        for r in self.m.curated.all_records():
            if r.memory_id in target_ids and r.status == "active":
                return False
        # semantic: vectors gone
        for tid in target_ids:
            for prefix in ("memory:", "journal:", "person:"):
                if f"{prefix}{tid}" in self.m.vectors:
                    return False
        return True

    # -- convenience --------------------------------------------------------------
    def forget(self, query: str, mode: str = "tombstone") -> ForgetResult:
        """Plan + execute in one call. Ambiguous targets are never deleted."""
        return self.execute(self.plan(query, mode))
