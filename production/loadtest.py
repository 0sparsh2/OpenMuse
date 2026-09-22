"""Load-test harness — Phase 8.

Scripted concurrency/latency targets with a pass/fail report, mirroring the
blueprint's exit criterion "Load tests meet product latency and concurrency
targets established before launch". The harness drives any workload
callable (API request path, queue submit/claim cycle, gateway completion)
with N threads for a fixed duration and reports throughput plus latency
percentiles against the declared targets.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from .models import LoadReport


@dataclass
class LoadTarget:
    max_p99_ms: float = 500.0
    min_rps: float = 10.0


class LoadHarness:
    def __init__(self, emit: Callable[[str, dict], None] | None = None):
        self.emit = emit or (lambda _t, _p: None)

    def run(self, name: str, workload: Callable[[], None], *,
            concurrency: int = 8, duration_s: float = 2.0,
            target: LoadTarget | None = None) -> LoadReport:
        target = target or LoadTarget()
        latencies: list[float] = []
        errors = 0
        lock = threading.Lock()
        stop = time.time() + duration_s

        def worker() -> None:
            nonlocal errors
            while time.time() < stop:
                start = time.perf_counter()
                try:
                    workload()
                except Exception:
                    with lock:
                        errors += 1
                finally:
                    ms = (time.perf_counter() - start) * 1000.0
                    with lock:
                        latencies.append(ms)

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(concurrency)]
        wall_start = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.time() - wall_start

        latencies.sort()
        calls = len(latencies)
        p50 = latencies[int(0.50 * (calls - 1))] if calls else 0.0
        p99 = latencies[int(0.99 * (calls - 1))] if calls else 0.0
        mx = latencies[-1] if calls else 0.0
        rps = calls / wall if wall > 0 else 0.0
        passed = (errors == 0 and p99 <= target.max_p99_ms
                  and rps >= target.min_rps)
        report = LoadReport(
            name=name, concurrency=concurrency, duration_s=wall,
            calls=calls, throughput_rps=rps, p50_ms=p50, p99_ms=p99,
            max_ms=mx, errors=errors,
            target_p99_ms=target.max_p99_ms, target_min_rps=target.min_rps,
            passed=passed)
        self.emit("loadtest.report", report.to_dict())
        return report
