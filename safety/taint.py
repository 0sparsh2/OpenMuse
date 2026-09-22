"""Source-to-sink taint tracking (Phase 10).

Data from untrusted sources — web pages, email, PDFs, child-agent output —
is tainted at ingestion. When tainted data reaches a sensitive sink
(external send, credential use, irreversible action, browser commit), the
call requires explicit re-authorization (taint clearance recorded against
the exact tainted artifact) or it is blocked.

Taint is tracked as explicit provenance, not string matching: the harness
ingests each untrusted artifact once and propagates its taint id alongside
the derived arguments. Clearance is per-artifact and recorded; a bound
approval whose bind fields disclose the taint provenance counts as clearance
for that call.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Untrusted source kinds. "user" and "system" are trusted and never tainted.
UNTRUSTED_SOURCES = ("web", "email", "pdf", "child_output")

# Sensitive sink kinds.
SINK_KINDS = ("external_send", "credential_use", "irreversible", "browser_commit")

# Tool -> sink mapping. Tools not listed have no sensitive sink.
# (side_effect == "external_write" is the general external_send rule;
#  this table pins the explicit cases used by the red-team corpus.)
TOOL_SINKS: dict[str, tuple[str, ...]] = {
    "connector.github.star": ("external_send",),
    "connector.github.unstar": ("external_send",),
    "production.channel_send": ("external_send",),
    "browser.act": ("browser_commit",),  # only when kind=confirm_commit; checked below
}


def sinks_for(tool_name: str, arguments: dict, side_effect: str = "") -> tuple[str, ...]:
    """Sensitive sinks a call targets. Deterministic and conservative."""
    sinks: list[str] = []
    explicit = TOOL_SINKS.get(tool_name, ())
    for s in explicit:
        if s == "browser_commit" and arguments.get("kind") != "confirm_commit":
            continue
        sinks.append(s)
    if side_effect == "external_write" and "external_send" not in sinks:
        sinks.append("external_send")
    if side_effect == "destructive" and "irreversible" not in sinks:
        sinks.append("irreversible")
    caps = arguments.get("_capabilities", [])  # harness-provided, never model-provided
    if "credential.use" in caps and "credential_use" not in sinks:
        sinks.append("credential_use")
    return tuple(sinks)


@dataclass
class TaintSource:
    taint_id: str
    kind: str            # web | email | pdf | child_output
    origin: str          # URL, message id, document id, child run id
    ingested_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class TaintClearance:
    taint_id: str
    cleared_by: str      # "user" — only the user clears taint
    note: str
    cleared_at: float = field(default_factory=time.time)


@dataclass
class TaintFinding:
    tainted: bool
    sink: str = ""
    taint_ids: tuple[str, ...] = ()
    cleared: bool = False
    reason_code: str = ""

    @property
    def blocked(self) -> bool:
        return self.tainted and not self.cleared


class TaintTracker:
    """Registry of tainted artifacts and their clearances."""

    def __init__(self):
        self._sources: dict[str, TaintSource] = {}
        self._clearances: dict[str, TaintClearance] = {}

    def ingest(self, kind: str, origin: str) -> str:
        """Taint an untrusted artifact at ingestion. Returns the taint id."""
        if kind not in UNTRUSTED_SOURCES:
            raise ValueError(f"kind {kind!r} is not an untrusted source")
        taint_id = "tnt_" + uuid.uuid4().hex[:12]
        self._sources[taint_id] = TaintSource(taint_id=taint_id, kind=kind, origin=origin)
        return taint_id

    def is_tainted(self, taint_id: str) -> bool:
        return taint_id in self._sources and taint_id not in self._clearances

    def provenance(self, taint_id: str) -> dict:
        src = self._sources.get(taint_id)
        if src is None:
            return {"taint_id": taint_id, "known": False}
        return {
            "taint_id": src.taint_id, "kind": src.kind, "origin": src.origin,
            "ingested_at": src.ingested_at,
            "cleared": taint_id in self._clearances,
        }

    def clear(self, taint_id: str, *, cleared_by: str, note: str) -> TaintClearance:
        """Explicit re-authorization. Only the user (or a user-bound approval)
        may clear taint — never the agent itself."""
        if cleared_by != "user":
            raise ValueError("taint clearance requires cleared_by='user'")
        if taint_id not in self._sources:
            raise ValueError(f"unknown taint id {taint_id!r}")
        clearance = TaintClearance(taint_id=taint_id, cleared_by=cleared_by, note=note)
        self._clearances[taint_id] = clearance
        return clearance

    def check_call(
        self,
        *,
        tool_name: str,
        arguments: dict,
        taint_ids: tuple[str, ...] = (),
        side_effect: str = "",
        risk: str = "R0",
    ) -> TaintFinding:
        """Does this call carry tainted data into a sensitive sink?"""
        live = tuple(t for t in taint_ids if self.is_tainted(t))
        if not live:
            return TaintFinding(tainted=False)
        for sink in sinks_for(tool_name, arguments, side_effect):
            if sink in ("external_send", "credential_use", "irreversible", "browser_commit"):
                code = "TAINTED_SINK_R4" if risk in ("R4", "R5") else "TAINTED_SINK"
                return TaintFinding(
                    tainted=True, sink=sink, taint_ids=live,
                    cleared=False, reason_code=code,
                )
        return TaintFinding(tainted=False)
