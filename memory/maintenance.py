"""
Background memory maintenance — interfaces only (Phase 2).

The maintenance job runs on a schedule (Phase 5 cron) or after N turns: it
receives completed turns plus a snapshot of existing memory and proposes a
change set. The runtime — never the model, never the maintainer directly —
applies the change set through the Consolidator.

Concrete LLM-backed maintainers arrive with the scheduler; this module
defines the contract and a deterministic heuristic maintainer used by the
demo and tests.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .consolidation import Consolidator, extract_candidates
from .records import ChangeOp, MemoryCandidate


@dataclass
class MaintenanceInput:
    """Completed turns since the last run + a memory snapshot summary."""
    turns: list[dict] = field(default_factory=list)  # [{role, text, ref}]
    memory_summary: str = ""


@dataclass
class ChangeSet:
    ops: list[ChangeOp] = field(default_factory=list)
    candidates: list[MemoryCandidate] = field(default_factory=list)
    maintainer: str = ""
    notes: str = ""


class MemoryMaintainer(ABC):
    """Proposes memory changes. Does not write; the runtime applies."""

    name: str = "maintainer-base"

    @abstractmethod
    def propose(self, inp: MaintenanceInput) -> ChangeSet:
        ...


class HeuristicMaintainer(MemoryMaintainer):
    """Deterministic maintainer: extraction heuristics + consolidation.

    Mirrors what the LLM maintenance job (prompts/memory-maintenance.md)
    would propose for explicit statements, without calling a model.
    """

    name = "heuristic-maintainer@1.0.0"

    def propose(self, inp: MaintenanceInput) -> ChangeSet:
        candidates: list[MemoryCandidate] = []
        for turn in inp.turns:
            if turn.get("role") != "user":
                continue
            candidates.extend(extract_candidates(turn.get("text", ""),
                                                 source_ref=turn.get("ref", "")))
        return ChangeSet(candidates=candidates, maintainer=self.name,
                         notes=f"{len(candidates)} candidates extracted")


def apply_changeset(consolidator: Consolidator, changeset: ChangeSet,
                    candidates: list[MemoryCandidate]) -> list[ChangeOp]:
    """Runtime-side application: consolidate candidates, return applied ops."""
    return consolidator.consolidate(candidates)
