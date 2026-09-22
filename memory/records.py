"""
Layered memory record types.

Mirrors the blueprint's structured durable memory schema ("Layer 2: curated
durable memory"), the episodic journal ("Layer 3"), the derivation graph
("Forget graph"), and the summary schema ("Layer 6: conversation summaries").

The Markdown files (MEMORY.md, journal pages, people pages) are projections.
These dataclasses are the authoritative structured records; projections are
regenerated from them, never the other way around.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
import secrets


def new_memory_id() -> str:
    return "mem_" + secrets.token_hex(8)


def new_id(prefix: str) -> str:
    return f"{prefix}_" + secrets.token_hex(8)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Curated durable records (Layer 2)
# ---------------------------------------------------------------------------
@dataclass
class MemoryRecord:
    """One structured durable memory, per the blueprint's record schema."""
    memory_id: str
    kind: str  # stable_fact | preference | commitment | relationship_update | operating_lesson
    subject: str = "user"
    predicate: str = ""
    claim: str = ""  # human-readable statement of the fact
    value: dict = field(default_factory=dict)  # normalized value
    scope: str = ""
    valid_from: str = ""
    valid_to: Optional[str] = None
    status: str = "active"  # active | superseded | deleted | tombstoned
    confidence: float = 0.9
    sensitivity: str = "personal"  # public | personal | sensitive
    source_refs: list = field(default_factory=list)
    supersedes: Optional[str] = None
    superseded_by: Optional[str] = None
    history: list = field(default_factory=list)  # prior values on refine
    embedding_ref: str = ""
    created_by: str = "memory_consolidator@1.0.0"
    created_at: str = ""
    reviewed_by_user: bool = False

    def __post_init__(self):
        if not self.created_at:
            self.created_at = utcnow()

    def to_dict(self) -> dict:
        return {
            "memory_id": self.memory_id, "kind": self.kind, "subject": self.subject,
            "predicate": self.predicate, "claim": self.claim, "value": self.value,
            "scope": self.scope, "valid_from": self.valid_from, "valid_to": self.valid_to,
            "status": self.status, "confidence": self.confidence,
            "sensitivity": self.sensitivity, "source_refs": self.source_refs,
            "supersedes": self.supersedes, "superseded_by": self.superseded_by,
            "history": self.history, "embedding_ref": self.embedding_ref,
            "created_by": self.created_by, "created_at": self.created_at,
            "reviewed_by_user": self.reviewed_by_user,
        }

    @staticmethod
    def from_dict(d: dict) -> "MemoryRecord":
        return MemoryRecord(**{k: d.get(k, v) for k, v in {
            "memory_id": "", "kind": "stable_fact", "subject": "user", "predicate": "",
            "claim": "", "value": {}, "scope": "", "valid_from": "", "valid_to": None,
            "status": "active", "confidence": 0.9, "sensitivity": "personal",
            "source_refs": [], "supersedes": None, "superseded_by": None,
            "history": [], "embedding_ref": "", "created_by": "memory_consolidator@1.0.0",
            "created_at": "", "reviewed_by_user": False,
        }.items()})


# ---------------------------------------------------------------------------
# Episodic journal entries (Layer 3)
# ---------------------------------------------------------------------------
@dataclass
class JournalEntry:
    entry_id: str
    timestamp: str
    title: str
    text: str
    event_refs: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    embedding_ref: str = ""

    def to_dict(self) -> dict:
        return {"entry_id": self.entry_id, "timestamp": self.timestamp,
                "title": self.title, "text": self.text,
                "event_refs": self.event_refs, "tags": self.tags,
                "embedding_ref": self.embedding_ref}

    @staticmethod
    def from_dict(d: dict) -> "JournalEntry":
        return JournalEntry(entry_id=d.get("entry_id", ""), timestamp=d.get("timestamp", ""),
                            title=d.get("title", ""), text=d.get("text", ""),
                            event_refs=d.get("event_refs", []), tags=d.get("tags", []),
                            embedding_ref=d.get("embedding_ref", ""))


# ---------------------------------------------------------------------------
# Conversation summaries (Layer 6)
# ---------------------------------------------------------------------------
@dataclass
class SummaryRecord:
    summary_id: str
    chat_id: str
    from_sequence: int
    to_sequence: int
    decisions: list = field(default_factory=list)
    commitments: list = field(default_factory=list)
    stable_facts: list = field(default_factory=list)
    open_threads: list = field(default_factory=list)
    safety_relevant: list = field(default_factory=list)
    omissions: list = field(default_factory=list)
    prompt_version: str = "compactor@1.0.0"
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = utcnow()


# ---------------------------------------------------------------------------
# Derivation graph (forget graph)
# ---------------------------------------------------------------------------
@dataclass
class DerivationEdge:
    """One edge: from_kind/from_id -[relation]-> to_kind/to_id.

    Relations: extracted_from, consolidated_into, projected_into,
    summarized_into, chunked_into, embedded_as, indexed_into.
    """
    from_kind: str
    from_id: str
    to_kind: str
    to_id: str
    relation: str
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = utcnow()

    def to_dict(self) -> dict:
        return {"from_kind": self.from_kind, "from_id": self.from_id,
                "to_kind": self.to_kind, "to_id": self.to_id,
                "relation": self.relation, "created_at": self.created_at}

    @staticmethod
    def from_dict(d: dict) -> "DerivationEdge":
        return DerivationEdge(d["from_kind"], d["from_id"], d["to_kind"],
                              d["to_id"], d["relation"], d.get("created_at", ""))


# ---------------------------------------------------------------------------
# Consolidation candidates and change sets
# ---------------------------------------------------------------------------
@dataclass
class MemoryCandidate:
    """Proposed memory from the extractor (deterministic or LLM)."""
    kind: str
    claim: str
    predicate: str = ""
    normalized: dict = field(default_factory=dict)
    scope: str = ""
    source_refs: list = field(default_factory=list)
    confidence: float = 0.8
    durability: str = "long_term"  # long_term | episodic_only
    sensitivity: str = "personal"
    action_hint: str = "upsert"  # upsert | journal_only | reject


@dataclass
class ChangeOp:
    """One applied change from the consolidator/maintainer."""
    op: str  # add | reinforce | refine | supersede | journal_only | reject | delete | tombstone
    memory_id: Optional[str] = None
    detail: str = ""
    candidate: Optional[MemoryCandidate] = None
