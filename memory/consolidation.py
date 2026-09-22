"""
Memory write pipeline: candidate extraction + consolidation.

Two paths produce candidates:
  1. Deterministic heuristics (this module) — conservative patterns for
     explicit durable statements. Safe to run inline during a turn.
  2. LLM extractor/consolidator (prompts/memory-extractor.md,
     prompts/memory-consolidator.md) — richer, runs in the background
     maintenance job. The runtime, not the model, performs writes.

Consolidation rules (per blueprint):
  - Exact duplicate        -> reinforce (attach source ref, no new row).
  - Refinement             -> update value, preserve history.
  - Contradiction          -> new row supersedes the old; old kept, marked.
  - Temporary/episodic     -> journal_only.
  - Sensitive category     -> store only if explicitly stated and necessary.
  - Secret-bearing         -> reject outright, never persisted.

A question is never recorded as ownership or a decision: candidates whose
claim is phrased as a question are rejected.
"""
from __future__ import annotations

import re
from typing import Optional

from tools.redaction import looks_like_secret

from .curated import CuratedMemory
from .records import ChangeOp, MemoryCandidate, MemoryRecord, new_memory_id, utcnow

_ALLOWED_KINDS = {"stable_fact", "preference", "commitment",
                  "relationship_update", "operating_lesson"}

# Conservative explicit-statement patterns: (regex, kind, predicate_builder)
_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\bremember that (.+?)[.!]?\s*$", re.I), "stable_fact", ""),
    (re.compile(r"\bmy (\w[\w\- ]{1,40}?) is (.+?)[.!]?\s*$", re.I), "stable_fact", ""),
    (re.compile(r"\bi prefer (.+?)[.!]?\s*$", re.I), "preference", ""),
    (re.compile(r"\bi (like|love|enjoy) (.+?)[.!]?\s*$", re.I), "preference", ""),
    (re.compile(r"\bi (hate|dislike|don't like|do not like) (.+?)[.!]?\s*$", re.I), "preference", ""),
    (re.compile(r"\bremind me to (.+?)[.!]?\s*$", re.I), "commitment", ""),
]


def _looks_temporary(claim: str) -> bool:
    return bool(re.search(r"\b(today|tonight|right now|for now|this week)\b", claim, re.I))


def _is_question(text: str) -> bool:
    return text.strip().endswith("?")


def extract_candidates(text: str, source_ref: str = "") -> list[MemoryCandidate]:
    """Deterministic candidate extraction from one turn's text.

    Conservative by design: only explicit durable statements qualify.
    Returns [] for questions, secrets, and chit-chat.
    """
    out: list[MemoryCandidate] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip().strip("-> ").strip()
        if not line or len(line) < 8 or _is_question(line):
            continue
        if looks_like_secret(line):
            continue
        for rx, kind, _ in _PATTERNS:
            m = rx.search(line)
            if not m:
                continue
            groups = [g for g in m.groups() if g]
            if kind == "stable_fact" and len(groups) >= 2 and rx.pattern.startswith("\\bmy "):
                attr, val = groups[0].strip(), groups[1].strip()
                claim = f"User's {attr} is {val}."
                predicate = "user_" + re.sub(r"\W+", "_", attr.lower()).strip("_")
                normalized = {"value": val}
            elif kind == "commitment":
                claim = f"User asked to be reminded to {groups[-1].strip()}."
                predicate, normalized = "reminder", {"task": groups[-1].strip()}
            else:
                claim_text = groups[-1].strip() if len(groups) > 1 else groups[0].strip()
                claim = line if len(line) < 160 else claim_text
                predicate = {"preference": "user_preference"}.get(kind, "user_fact")
                normalized = {"value": claim_text}
            if _looks_temporary(claim):
                out.append(MemoryCandidate(kind="stable_fact", claim=claim,
                                           predicate=predicate, normalized=normalized,
                                           source_refs=[source_ref] if source_ref else [],
                                           durability="episodic_only", action_hint="journal_only"))
            else:
                out.append(MemoryCandidate(kind=kind, claim=claim, predicate=predicate,
                                           normalized=normalized,
                                           source_refs=[source_ref] if source_ref else [],
                                           confidence=0.85, action_hint="upsert"))
            break
    # de-dupe identical claims within the batch
    seen, deduped = set(), []
    for c in out:
        if c.claim.lower() not in seen:
            seen.add(c.claim.lower())
            deduped.append(c)
    return deduped


class Consolidator:
    """Applies candidates to curated memory per the blueprint's rules."""

    def __init__(self, curated: CuratedMemory):
        self.curated = curated

    def consolidate(self, candidates: list[MemoryCandidate],
                    created_by: str = "memory_consolidator@1.0.0") -> list[ChangeOp]:
        ops: list[ChangeOp] = []
        for cand in candidates:
            if cand.kind not in _ALLOWED_KINDS:
                ops.append(ChangeOp(op="reject", detail=f"unknown kind {cand.kind!r}",
                                    candidate=cand))
                continue
            if cand.action_hint == "reject":
                ops.append(ChangeOp(op="reject", detail="rejected by extractor",
                                    candidate=cand))
                continue
            if cand.action_hint == "journal_only" or cand.durability == "episodic_only":
                ops.append(ChangeOp(op="journal_only",
                                    detail="episodic; kept in journal only",
                                    candidate=cand))
                continue
            ops.append(self._upsert(cand, created_by))
        return ops

    def _upsert(self, cand: MemoryCandidate, created_by: str) -> ChangeOp:
        siblings = self.curated.find_by_predicate(cand.predicate, cand.kind)
        norm_val = _norm_value(cand.normalized)

        # Exact duplicate -> reinforce.
        for s in siblings:
            if _norm_value(s.value) == norm_val:
                if cand.source_refs:
                    for ref in cand.source_refs:
                        if ref not in s.source_refs:
                            s.source_refs.append(ref)
                s.confidence = min(0.99, s.confidence + 0.02)
                self.curated.update(s)
                return ChangeOp(op="reinforce", memory_id=s.memory_id,
                                detail="duplicate claim; source ref attached",
                                candidate=cand)

        # Refinement: same predicate, new value extends the old one.
        for s in siblings:
            old_v, new_v = _norm_value(s.value), norm_val
            if old_v and new_v and (new_v.startswith(old_v) or old_v.startswith(new_v)) \
                    and old_v != new_v:
                s.history.append({"value": dict(s.value), "at": utcnow()})
                s.value = dict(cand.normalized)
                s.claim = cand.claim
                if cand.source_refs:
                    s.source_refs.extend(r for r in cand.source_refs if r not in s.source_refs)
                self.curated.update(s)
                return ChangeOp(op="refine", memory_id=s.memory_id,
                                detail="value refined; history preserved", candidate=cand)

        # Contradiction: same predicate, different value -> supersede.
        if siblings and norm_val:
            prev = sorted(siblings, key=lambda r: r.created_at)[-1]
            record = MemoryRecord(
                memory_id=new_memory_id(), kind=cand.kind, predicate=cand.predicate,
                claim=cand.claim, value=dict(cand.normalized), scope=cand.scope,
                confidence=cand.confidence, sensitivity=cand.sensitivity,
                source_refs=list(cand.source_refs), supersedes=prev.memory_id,
                created_by=created_by)
            self.curated.add(record)
            prev.status = "superseded"
            prev.superseded_by = record.memory_id
            self.curated.update(prev)
            return ChangeOp(op="supersede", memory_id=record.memory_id,
                            detail=f"contradicts {prev.memory_id}; old row superseded",
                            candidate=cand)

        # Fresh add.
        record = MemoryRecord(
            memory_id=new_memory_id(), kind=cand.kind, predicate=cand.predicate,
            claim=cand.claim, value=dict(cand.normalized), scope=cand.scope,
            confidence=cand.confidence, sensitivity=cand.sensitivity,
            source_refs=list(cand.source_refs), created_by=created_by)
        self.curated.add(record)
        return ChangeOp(op="add", memory_id=record.memory_id,
                        detail="new durable record", candidate=cand)


def _norm_value(value: dict) -> str:
    v = value.get("value", value.get("task", ""))
    return re.sub(r"\s+", " ", str(v).strip().lower())
