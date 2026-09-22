"""
Event log — every state change is appended as an immutable envelope.

Mirrors the blueprint's event envelope. Phase 1 keeps events in memory and
optionally mirrors them to JSONL. The materialized run row is a cache of the
latest state; recovery replays the log.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class Event:
    event_id: str
    run_id: str
    sequence: int
    type: str
    occurred_at: str
    actor: dict
    payload: dict
    payload_sha256: str


class EventLog:
    def __init__(self, run_id: str, *, jsonl_path: Optional[str] = None):
        self.run_id = run_id
        self.events: list[Event] = []
        self._jsonl = None
        if jsonl_path:
            self._jsonl = open(jsonl_path, "a", encoding="utf-8")

    def append(self, type: str, payload: dict, *, actor: dict | None = None) -> Event:
        seq = len(self.events)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        event = Event(
            event_id="evt_" + uuid.uuid4().hex[:12],
            run_id=self.run_id,
            sequence=seq,
            type=type,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            actor=actor or {"kind": "agent_worker", "id": "worker_phase1"},
            payload=payload,
            payload_sha256="sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )
        self.events.append(event)
        if self._jsonl:
            self._jsonl.write(json.dumps({
                "event_id": event.event_id, "run_id": event.run_id,
                "sequence": event.sequence, "type": event.type,
                "occurred_at": event.occurred_at, "actor": event.actor,
                "payload": payload, "payload_sha256": event.payload_sha256,
            }, ensure_ascii=False) + "\n")
            self._jsonl.flush()
        return event

    def of_type(self, type: str) -> list[Event]:
        return [e for e in self.events if e.type == type]

    def close(self) -> None:
        if self._jsonl:
            self._jsonl.close()
            self._jsonl = None
