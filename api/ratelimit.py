"""
Per-key token-bucket rate limiting with clear 429 semantics.

Each API key gets its own bucket (capacity + refill rate). When the bucket
is empty the request is rejected with RATE_LIMITED and a Retry-After hint
in whole seconds. Buckets are in-memory; a distributed deployment would
back this with a shared store behind the same interface.
"""
from __future__ import annotations

import math
import time


class TokenBucket:
    def __init__(self, capacity: int, refill_per_second: float):
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_second)
        self._tokens = float(capacity)
        self._updated = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
            self._updated = now

    def take(self) -> tuple[bool, float]:
        """Consume one token. Returns (allowed, retry_after_seconds)."""
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True, 0.0
        deficit = 1.0 - self._tokens
        retry_after = deficit / self.refill_per_second if self.refill_per_second > 0 else 60.0
        return False, math.ceil(retry_after)


class RateLimiter:
    def __init__(self):
        self._buckets: dict[str, TokenBucket] = {}

    def check(self, key_id: str, *, per_minute: int) -> tuple[bool, float]:
        bucket = self._buckets.get(key_id)
        if bucket is None or bucket.capacity != float(per_minute):
            bucket = TokenBucket(per_minute, per_minute / 60.0)
            self._buckets[key_id] = bucket
        return bucket.take()
