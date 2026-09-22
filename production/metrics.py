"""Per-tenant metrics feeding the observability log — Phase 8.

Counters and latency observations keyed by (tenant, metric). Every
recording can also emit a `metrics.sample` event into the platform's
existing EventLog, so production telemetry flows through the same
immutable event stream as everything else.
"""
from __future__ import annotations

import threading
from typing import Callable

from ._store import utcnow


class Metrics:
    def __init__(self, emit: Callable[[str, dict], None] | None = None):
        self._emit = emit
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, str], float] = {}
        self._timings: dict[tuple[str, str], list[float]] = {}

    def incr(self, tenant_id: str, name: str, value: float = 1.0) -> None:
        with self._lock:
            key = (tenant_id, name)
            self._counters[key] = self._counters.get(key, 0.0) + value
            total = self._counters[key]
        if self._emit:
            self._emit("metrics.sample", {
                "tenant_id": tenant_id, "metric": name, "kind": "counter",
                "value": value, "total": total, "at": utcnow()})

    def observe(self, tenant_id: str, name: str, value_ms: float) -> None:
        with self._lock:
            self._timings.setdefault((tenant_id, name), []).append(value_ms)
        if self._emit:
            self._emit("metrics.sample", {
                "tenant_id": tenant_id, "metric": name, "kind": "timing",
                "value_ms": value_ms, "at": utcnow()})

    def counter(self, tenant_id: str, name: str) -> float:
        with self._lock:
            return self._counters.get((tenant_id, name), 0.0)

    def timing_stats(self, tenant_id: str, name: str) -> dict:
        with self._lock:
            vals = sorted(self._timings.get((tenant_id, name), []))
        if not vals:
            return {"count": 0}
        return {"count": len(vals),
                "avg_ms": sum(vals) / len(vals),
                "max_ms": vals[-1],
                "p50_ms": vals[int(0.5 * (len(vals) - 1))]}

    def snapshot(self) -> dict:
        with self._lock:
            counters = {f"{t}/{n}": v
                        for (t, n), v in self._counters.items()}
            timings = {f"{t}/{n}": len(v)
                       for (t, n), v in self._timings.items()}
        return {"counters": counters, "timing_series": timings}
