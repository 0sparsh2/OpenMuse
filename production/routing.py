"""Tenant-aware provider routing with compatible fallbacks — Phase 8.

Each tenant gets an ordered provider list. A fallback is used only when
its data-handling policy is compatible with the run's data class
(blueprint: "A fallback is permitted only when its data-processing and
regional policies are compatible with the run. Do not send sensitive
content to a new provider merely because the primary is unavailable.").

The router builds on the gateway's Router (which already implements
transient/rate-limit failover and per-(run, step) idempotency); this layer
adds the per-tenant ordering and the compatibility gate. Every routing
decision is recorded for observability.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from gateway import Router
from gateway.protocol import ModelRequest, ModelResponse, Provider

from ._store import utcnow


@dataclass
class TenantRoutingPolicy:
    provider_order: list[str] = field(default_factory=list)
    allow_fallback: bool = True


class IncompatibleFallback(Exception):
    pass


class _TracingProvider(Provider):
    """Delegates to a real provider while recording that it served."""

    def __init__(self, inner: Provider, label: str):
        self._inner = inner
        self.label = label
        self.name = getattr(inner, "name", label)
        self.served = 0

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.served += 1
        return self._inner.complete(request)


def _compatible(data_policy: dict, data_class: str) -> bool:
    """Gate: sensitive data only goes to providers whose policy allows it."""
    if data_class == "pii":
        return bool(data_policy.get("pii_ok", False))
    if data_class == "eu":
        return data_policy.get("residency", "global") in ("eu", "global")
    return True  # "public" may use any registered provider


class TenantRouter:
    def __init__(self):
        self._providers: dict[str, tuple[Provider, dict]] = {}
        self._policies: dict[str, TenantRoutingPolicy] = {}
        self._routers: dict[tuple[str, str], Router] = {}
        self.decisions: list[dict] = []

    def register_provider(self, name: str, provider: Provider,
                          *, data_policy: dict | None = None) -> None:
        self._providers[name] = (provider, data_policy or {})
        self._routers.clear()

    def set_tenant_policy(self, tenant_id: str,
                          policy: TenantRoutingPolicy) -> None:
        self._policies[tenant_id] = policy
        self._routers = {k: v for k, v in self._routers.items()
                         if k[0] != tenant_id}

    def _named_providers_for(self, tenant_id: str,
                               data_class: str = "public"
                               ) -> list[tuple[str, Provider]]:
        policy = self._policies.get(tenant_id, TenantRoutingPolicy(
            provider_order=list(self._providers)))
        ordered = [n for n in policy.provider_order if n in self._providers]
        if not ordered:
            raise IncompatibleFallback(
                f"tenant {tenant_id!r} has no registered providers")
        compatible: list[tuple[str, Provider]] = []
        skipped: list[str] = []
        for name in ordered:
            provider, data_policy = self._providers[name]
            if _compatible(data_policy, data_class):
                compatible.append((name, provider))
                if not policy.allow_fallback:
                    break
            else:
                skipped.append(name)
        if not compatible:
            raise IncompatibleFallback(
                f"no compatible provider for tenant {tenant_id!r} "
                f"with data class {data_class!r} (skipped: {skipped})")
        return compatible

    def providers_for(self, tenant_id: str,
                      data_class: str = "public") -> list[Provider]:
        return [p for _, p in self._named_providers_for(tenant_id,
                                                       data_class)]

    def complete(self, tenant_id: str, request: ModelRequest, *,
                 data_class: str = "public",
                 idempotency_key: str) -> ModelResponse:
        key = (tenant_id, data_class)
        router = self._routers.get(key)
        traced: list[_TracingProvider] = []
        if router is None:
            for name, provider in self._named_providers_for(tenant_id,
                                                            data_class):
                t = _TracingProvider(provider, name)
                traced.append(t)
            router = Router({request.model_class: traced})
            self._routers[key] = (router, traced)
        else:
            router, traced = router
        before = [t.served for t in traced]
        response = router.complete(request, idempotency_key=idempotency_key)
        winner = "unknown"
        for t, b in zip(traced, before):
            if t.served > b:
                winner = t.label
        self.decisions.append({
            "tenant_id": tenant_id, "data_class": data_class,
            "model_class": request.model_class,
            "provider": winner,
            "idempotency_key": idempotency_key, "at": utcnow(),
        })
        return response
