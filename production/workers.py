"""Worker pool — Phase 8.

Heavy work runs here, not in the API process. Workers claim messages from
healthy regions, execute them through an injected executor callable, and
ack/nack the queue. Execution is exactly-once per message id: a completed
set is consulted before executing, so a replayed or re-queued message can
never run its side effects twice (queue-level dedup in RegionalQueue is the
primary guard; this is the defensive second layer).

Retryable executor failures nack the message (requeue with attempts+1);
fatal failures and exhausted attempts go to the dead-letter state with a
reason. Optional quota enforcement sheds or defers a tenant's work before
any execution happens (backpressure).
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from ._store import utcnow
from .models import CLAIMED, COMPLETED, DEAD, QUEUED, WorkItem
from .queue import RegionalQueue, RegionDown


class RetryableError(Exception):
    """The executor failed transiently; the message may be retried."""


class FatalError(Exception):
    """The executor failed terminally; do not retry."""


class WorkerPool:
    def __init__(
        self,
        queue: RegionalQueue,
        executor: Callable[[WorkItem], dict],
        *,
        worker_count: int = 2,
        regions: list[str] | None = None,
        quota_manager=None,
        emit: Callable[[str, dict], None] | None = None,
        poll_interval_s: float = 0.01,
        name_prefix: str = "worker",
    ):
        self.queue = queue
        self.executor = executor
        self.worker_count = worker_count
        self.regions = regions or queue.regions
        self.quota_manager = quota_manager
        self.emit = emit or (lambda _t, _p: None)
        self.poll_interval_s = poll_interval_s
        self.name_prefix = name_prefix
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._completed: set[str] = set()
        self._lock = threading.Lock()
        self.executed: list[str] = []   # message ids actually executed
        self.results: dict[str, dict] = {}

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        for i in range(self.worker_count):
            t = threading.Thread(
                target=self._loop, args=(f"{self.name_prefix}-{i}",),
                daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)
        self._threads = []

    def run_until_empty(self, timeout_s: float = 30) -> None:
        """Process until every region is drained (demo/test helper)."""
        self.start()
        try:
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                if all(self.queue.depth(r) == 0 for r in self.regions):
                    # give in-flight claims a beat to ack
                    time.sleep(0.05)
                    if all(self.queue.depth(r) == 0 for r in self.regions):
                        break
                time.sleep(0.02)
        finally:
            self.stop()

    # -- main loop -----------------------------------------------------------
    def _loop(self, worker_id: str) -> None:
        while not self._stop.is_set():
            claimed = False
            for region in self.regions:
                if not self.queue.is_healthy(region):
                    continue
                try:
                    item = self.queue.claim(region, worker_id)
                except RegionDown:
                    continue
                if item is None:
                    continue
                claimed = True
                self._process(region, worker_id, item)
            if not claimed:
                time.sleep(self.poll_interval_s)

    def _process(self, region: str, worker_id: str, item: WorkItem) -> None:
        # Defensive exactly-once: never re-execute a completed message.
        with self._lock:
            if item.message_id in self._completed:
                self.queue.ack(region, item.message_id)
                return
        # Backpressure before any execution.
        if self.quota_manager is not None:
            decision = self.quota_manager.check(
                item.tenant_id,
                tokens=item.payload.get("estimated_tokens", 0))
            if decision.action == "shed":
                self.queue.mark_dead(region, item.message_id, "quota_shed")
                self.emit("worker.shed", {
                    "message_id": item.message_id, "run_id": item.run_id,
                    "tenant_id": item.tenant_id, "reason": decision.reason,
                    "at": utcnow()})
                return
            if decision.action == "queue":
                self.queue.defer(region, item.message_id)
                self.emit("worker.deferred", {
                    "message_id": item.message_id, "run_id": item.run_id,
                    "tenant_id": item.tenant_id, "reason": decision.reason,
                    "at": utcnow()})
                return
        try:
            result = self.executor(item)
        except RetryableError as exc:
            outcome = self.queue.nack(region, item.message_id)
            self.emit("worker.retry", {
                "message_id": item.message_id, "run_id": item.run_id,
                "tenant_id": item.tenant_id, "error": str(exc),
                "outcome": outcome, "at": utcnow()})
            return
        except FatalError as exc:
            self.queue.mark_dead(region, item.message_id, f"fatal: {exc}")
            self.emit("worker.dead", {
                "message_id": item.message_id, "run_id": item.run_id,
                "tenant_id": item.tenant_id, "error": str(exc),
                "at": utcnow()})
            return
        with self._lock:
            self._completed.add(item.message_id)
            self.executed.append(item.message_id)
            self.results[item.message_id] = result
        if self.quota_manager is not None:
            self.quota_manager.record_usage(
                item.tenant_id,
                tokens=result.get("tokens_used", 0),
                provider=result.get("provider", ""))
        self.queue.ack(region, item.message_id)
        self.emit("worker.completed", {
            "message_id": item.message_id, "run_id": item.run_id,
            "tenant_id": item.tenant_id, "kind": item.kind,
            "attempts": item.attempts, "at": utcnow()})
