"""Per-tenant quotas, cost controls, and backpressure — Phase 8.

Each tenant gets call, token, and spend budgets inside a sliding window.
`check` runs before any execution; when a tenant is over budget the policy
decides the backpressure behavior: "shed" (reject now, dead-letter with a
reason) or "queue" (defer — the work waits until budget frees up).
`record_usage` accounts real consumption with per-provider unit costs.

Mirrors the blueprint's Phase 8 "Per-tenant quotas, cost controls ...
and backpressure".
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

from ._store import atomic_write_json, read_json, utcnow
from .models import QuotaDecision


@dataclass
class QuotaPolicy:
    max_calls: int = 1000
    max_tokens: int = 1_000_000
    max_spend: float = 50.0          # currency units per window
    window_s: float = 3600.0
    on_exceed: str = "shed"          # "shed" | "queue"
    unit_cost_per_1k: dict = field(default_factory=dict)  # provider -> cost


class QuotaManager:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._policies: dict[str, QuotaPolicy] = {}
        self._lock = threading.RLock()

    def _usage_path(self, tenant_id: str) -> str:
        return os.path.join(self.root, f"{tenant_id}.json")

    def set_policy(self, tenant_id: str, policy: QuotaPolicy) -> None:
        self._policies[tenant_id] = policy

    def policy_for(self, tenant_id: str) -> QuotaPolicy:
        return self._policies.get(tenant_id, QuotaPolicy())

    def _load_usage(self, tenant_id: str) -> list[dict]:
        return read_json(self._usage_path(tenant_id), [])

    def _save_usage(self, tenant_id: str, usage: list[dict]) -> None:
        atomic_write_json(self._usage_path(tenant_id), usage)

    def _window_usage(self, tenant_id: str) -> tuple[int, int, float]:
        policy = self.policy_for(tenant_id)
        cutoff = time.time() - policy.window_s
        usage = [u for u in self._load_usage(tenant_id) if u["ts"] >= cutoff]
        if len(usage) != len(self._load_usage(tenant_id)):
            self._save_usage(tenant_id, usage)
        calls = sum(u["calls"] for u in usage)
        tokens = sum(u["tokens"] for u in usage)
        spend = sum(u["cost"] for u in usage)
        return calls, tokens, spend

    def check(self, tenant_id: str, *, tokens: int = 0,
              provider: str = "") -> QuotaDecision:
        with self._lock:
            return self._check_locked(tenant_id, tokens=tokens,
                                      provider=provider)

    def _check_locked(self, tenant_id: str, *, tokens: int = 0,
                      provider: str = "") -> QuotaDecision:
        policy = self.policy_for(tenant_id)
        calls, used_tokens, spend = self._window_usage(tenant_id)
        est_cost = (tokens / 1000.0) * policy.unit_cost_per_1k.get(provider, 0.0)
        over = None
        if calls + 1 > policy.max_calls:
            over = f"call budget exceeded ({calls}/{policy.max_calls})"
        elif used_tokens + tokens > policy.max_tokens:
            over = f"token budget exceeded ({used_tokens}/{policy.max_tokens})"
        elif spend + est_cost > policy.max_spend:
            over = (f"spend cap exceeded "
                    f"({spend:.4f}/{policy.max_spend:.4f})")
        if over is None:
            return QuotaDecision(action="allow", reason="within budget",
                                 tenant_id=tenant_id)
        return QuotaDecision(action=policy.on_exceed,
                             reason=over, tenant_id=tenant_id)

    def record_usage(self, tenant_id: str, *, calls: int = 1,
                     tokens: int = 0, provider: str = "") -> dict:
        with self._lock:
            policy = self.policy_for(tenant_id)
            cost = (tokens / 1000.0) * policy.unit_cost_per_1k.get(provider, 0.0)
            usage = self._load_usage(tenant_id)
            usage.append({"ts": time.time(), "calls": calls,
                          "tokens": tokens, "cost": cost, "at": utcnow()})
            self._save_usage(tenant_id, usage)
            calls_w, tokens_w, spend_w = self._window_usage(tenant_id)
        return {"calls": calls_w, "tokens": tokens_w, "spend": spend_w,
                "policy": {"max_calls": policy.max_calls,
                           "max_tokens": policy.max_tokens,
                           "max_spend": policy.max_spend,
                           "window_s": policy.window_s}}

    def status(self, tenant_id: str) -> dict:
        policy = self.policy_for(tenant_id)
        calls, tokens, spend = self._window_usage(tenant_id)
        return {
            "tenant_id": tenant_id,
            "window_s": policy.window_s,
            "on_exceed": policy.on_exceed,
            "usage": {"calls": calls, "tokens": tokens,
                      "spend": round(spend, 4)},
            "budget": {"max_calls": policy.max_calls,
                       "max_tokens": policy.max_tokens,
                       "max_spend": policy.max_spend},
        }

    def reset(self, tenant_id: str) -> None:
        self._save_usage(tenant_id, [])
