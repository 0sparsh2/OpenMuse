"""
Per-run SSE event bus with sequence numbers and reconnect replay.

Each run gets an append-only event list. Every event carries a per-run
monotonic sequence number, exposed as the SSE `id:` field. A client that
disconnects reconnects with `Last-Event-ID: <seq>` (header or ?last_event_id=)
and receives every event with a higher sequence — no duplicated content, no
missed events. Events are retained for the process lifetime (durable backing
is a deployment concern behind the same interface).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class SseEvent:
    seq: int
    type: str
    data: dict
    occurred_at: float = field(default_factory=time.time)


class RunEventBus:
    def __init__(self):
        self._events: dict[str, list[SseEvent]] = {}

    def publish(self, run_id: str, type: str, data: dict) -> SseEvent:
        events = self._events.setdefault(run_id, [])
        evt = SseEvent(seq=len(events), type=type, data=data)
        events.append(evt)
        return evt

    def read_since(self, run_id: str, last_event_id: int) -> list[SseEvent]:
        """Events with seq > last_event_id. last_event_id=-1 returns all."""
        return [e for e in self._events.get(run_id, []) if e.seq > last_event_id]

    def count(self, run_id: str) -> int:
        return len(self._events.get(run_id, []))


def format_sse(evt: SseEvent) -> bytes:
    import json
    lines = [
        f"id: {evt.seq}",
        f"event: {evt.type}",
        f"data: {json.dumps(evt.data, ensure_ascii=False)}",
        "",
        "",
    ]
    return ("\n".join(lines)).encode("utf-8")
