"""Layered memory package (Phase 2)."""
from .store import MemoryStore
from .layered import LayeredMemory
from .curated import CuratedMemory
from .journal import Journal
from .people import PeopleIndex, AmbiguousPersonError
from .vector_index import VectorIndex
from .embeddings import EmbeddingProvider, DeterministicEmbedder, OpenAICompatibleEmbedder
from .consolidation import Consolidator, extract_candidates
from .forgetting import ForgettingService
from .derivation import DerivationGraph
from .working import WorkingMemory
from .maintenance import MemoryMaintainer, HeuristicMaintainer, ChangeSet
from .records import MemoryRecord, JournalEntry, MemoryCandidate, ChangeOp

__all__ = [
    "MemoryStore", "LayeredMemory", "CuratedMemory", "Journal",
    "PeopleIndex", "AmbiguousPersonError", "VectorIndex",
    "EmbeddingProvider", "DeterministicEmbedder", "OpenAICompatibleEmbedder",
    "Consolidator", "extract_candidates", "ForgettingService",
    "DerivationGraph", "WorkingMemory",
    "MemoryMaintainer", "HeuristicMaintainer", "ChangeSet",
    "MemoryRecord", "JournalEntry", "MemoryCandidate", "ChangeOp",
]
