"""Secret scanners gating persistence and egress (Phase 10).

Two layers:
  1. Generic secret shapes via tools.redaction (api keys, tokens, private
     keys, credential assignments).
  2. Seeded canaries: the red-team harness plants canary credentials with
     register_canary(); scan_persist/scan_egress block them even when they
     do not match any generic pattern — proving the scanner, not the regex,
     is what stops seeded-credential exfiltration.

scan_persist(text) -> gate persistence (files.write, memory.note, logs).
scan_egress(text)  -> gate external sends (channel_send, connector calls).
Both return a ScanResult; blocked results carry labels only, never values.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from tools.redaction import redact_text

_CANARIES: set[str] = set()


def seed_canary(value: str) -> str:
    """Register a canary credential for the red-team harness. Returns it."""
    _CANARIES.add(value)
    return value


def clear_canaries() -> None:
    _CANARIES.clear()


@dataclass
class ScanResult:
    blocked: bool
    labels: tuple[str, ...] = ()
    redacted_text: str = ""
    reason_code: str = ""  # "" | SECRET_PERSIST_BLOCKED | SECRET_EGRESS_BLOCKED


def _scan(text: str, *, egress: bool) -> ScanResult:
    labels: list[str] = []
    redacted, found = redact_text(text or "")
    labels.extend(found)
    for canary in _CANARIES:
        if canary and canary in (text or ""):
            labels.append("seeded_canary")
            redacted = redacted.replace(canary, "[REDACTED:seeded_canary]")
    labels = sorted(set(labels))
    if labels:
        return ScanResult(
            blocked=True, labels=tuple(labels), redacted_text=redacted,
            reason_code="SECRET_EGRESS_BLOCKED" if egress else "SECRET_PERSIST_BLOCKED",
        )
    return ScanResult(blocked=False, redacted_text=text or "")


def scan_persist(text: str) -> ScanResult:
    """Gate for persistence sinks: files, memory, logs, backups."""
    return _scan(text, egress=False)


def scan_egress(text: str) -> ScanResult:
    """Gate for egress sinks: external sends and connector calls."""
    return _scan(text, egress=True)


class SecretScanner:
    """Stateful scanner used by the demo harness (wraps the functions)."""

    def __init__(self):
        self.blocks: list[dict] = []

    def scan_persist(self, text: str, *, context: str = "") -> ScanResult:
        res = scan_persist(text)
        if res.blocked:
            self.blocks.append({"gate": "persist", "context": context, "labels": list(res.labels)})
        return res

    def scan_egress(self, text: str, *, context: str = "") -> ScanResult:
        res = scan_egress(text)
        if res.blocked:
            self.blocks.append({"gate": "egress", "context": context, "labels": list(res.labels)})
        return res
