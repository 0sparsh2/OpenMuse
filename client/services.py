"""Phase 9 — client surface wrappers over backend services.

Each class presents one client surface (scheduler/hook manager, memory
viewer/editor, connector management, browser handoff, usage/cost/privacy)
in terms of plain dicts safe for UI rendering. Secret material never
crosses into the client: vault capture flows return capture URLs/opaque
refs, and credential values are never resolved client-side.
"""
from __future__ import annotations

import os
from dataclasses import asdict, is_dataclass

from .state import assert_no_secrets


def _safe(obj):
    """Convert dataclasses to dicts; drop secret-valued keys."""
    if is_dataclass(obj):
        obj = asdict(obj)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if kl == "signing_secret" or kl.endswith("_secret") \
                    or "secret_value" in kl or "raw_key" in kl:
                continue
            out[k] = _safe(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_safe(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Schedules & hooks
# ---------------------------------------------------------------------------
class ScheduleClient:
    """Schedule/hook management UI surface."""

    def __init__(self, service):
        self.service = service

    def list_schedules(self) -> list[dict]:
        return [_safe(s) for s in self.service.list_schedules()]

    def create_schedule(self, *, name: str, schedule: str, timezone: str,
                        instructions: str, kind: str = "cron") -> dict:
        sched = self.service.create_schedule(
            name=name, schedule=schedule, timezone=timezone,
            instructions=instructions, kind=kind,
            run_at=schedule if kind == "once" else "")
        return _safe(sched)

    def get_schedule(self, schedule_id: str) -> dict:
        return _safe(self.service.get_schedule(schedule_id))

    def update_schedule(self, schedule_id: str, **changes) -> dict:
        return _safe(self.service.update_schedule(schedule_id, **changes))

    def set_enabled(self, schedule_id: str, enabled: bool) -> dict:
        return _safe(self.service.set_enabled(schedule_id, enabled))

    def remove_schedule(self, schedule_id: str) -> None:
        self.service.remove_schedule(schedule_id)

    def preview(self, schedule_id: str, n: int = 5) -> list[str]:
        return self.service.preview(schedule_id, n=n)

    def run_now(self, schedule_id: str) -> dict:
        return _safe(self.service.run_now(schedule_id))

    # -- hooks --------------------------------------------------------------
    def list_hooks(self) -> list[dict]:
        return [_safe(h) for h in self.service.list_hooks()]

    def create_hook(self, *, name: str, provider: str, event_type: str,
                    instructions: str,
                    delivery_policy: dict | None = None) -> dict:
        hook = self.service.create_hook(
            name=name, provider=provider, event_type=event_type,
            instructions=instructions, delivery_policy=delivery_policy)
        return _safe(hook)

    def remove_hook(self, hook_id: str) -> None:
        self.service.remove_hook(hook_id)


# ---------------------------------------------------------------------------
# Memory viewer / editor
# ---------------------------------------------------------------------------
class MemoryClient:
    """Memory viewer/editor surface with forget controls."""

    def __init__(self, layered_memory):
        self.memory = layered_memory

    def stats(self) -> dict:
        return _safe(self.memory.stats())

    def recall(self, query: str, top_k: int = 5) -> list[dict]:
        return _safe(self.memory.recall(query, top_k=top_k))

    def remember(self, text: str, *, source_ref: str = "") -> dict:
        assert_no_secrets(text, label="memory entry")
        return _safe(self.memory.remember(text, source_ref=source_ref))

    def memory_md_path(self) -> str:
        return self.memory.memory_md()

    def people(self) -> list[dict]:
        out = []
        for p in self.memory.people.list_people():
            out.append(_safe(p))
        return out

    def person_page(self, name: str) -> str:
        """Read a person page markdown (source links preserved)."""
        slug = "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")
        pages_dir = self.memory.people._pages_dir
        for fname in (f"{slug}.md",):
            path = os.path.join(pages_dir, fname)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
        return ""

    def forget_plan(self, query: str, *, mode: str = "tombstone") -> dict:
        """Preview the forgetting plan; nothing is removed yet."""
        plan = self.memory.forgetting.plan(query, mode=mode)
        return _safe(plan)

    def forget(self, query: str, *, mode: str = "tombstone") -> dict:
        """Execute the forgetting pipeline (plan -> execute -> verify)."""
        result = self.memory.forget(query, mode=mode)
        return _safe(result)


# ---------------------------------------------------------------------------
# Connector management
# ---------------------------------------------------------------------------
class ConnectorClient:
    """Connector catalog, connect/disconnect, scopes, Secure Vault capture.

    The client never handles raw secrets: credential capture returns a
    capture URL plus an opaque capture id for the Secure Vault capture
    page; the secret is entered there (server-side vault), and the client
    polls for completion which reports only status — never the value.
    """

    def __init__(self, registry):
        self.registry = registry

    def catalog(self) -> list[dict]:
        out = []
        for provider in self._providers():
            m = self.registry.manifest(provider)
            out.append({
                "provider": provider,
                "display_name": getattr(m, "display_name", provider),
                "auth_kinds": list(getattr(m, "auth_kinds", [])),
                "scopes": list(getattr(m, "scopes", [])),
                "operations": [op.name for op in getattr(m, "operations", [])],
            })
        return out

    def _providers(self) -> list[str]:
        adapters = getattr(self.registry, "_adapters", {})
        return sorted(adapters.keys())

    def begin_connect(self, *, tenant_id: str, provider: str,
                      auth_kind: str, scopes: list[str],
                      account_label: str = "") -> dict:
        """OAuth connect: returns authorization_url + state. The user
        completes authorization in their browser; raw tokens never touch
        this client."""
        result = self.registry.connect(
            tenant_id=tenant_id, provider=provider, auth_kind=auth_kind,
            scopes=scopes, account_label=account_label)
        safe = _safe(result)
        assert_no_secrets(safe, label="connector connect response")
        return safe

    def complete_authorization(self, *, tenant_id: str, state: str,
                               code: str = "", error: str = "") -> dict:
        return _safe(self.registry.complete_authorization(
            tenant_id=tenant_id, state=state))

    def begin_credential_capture(self, *, tenant_id: str, provider: str,
                                 purpose: str) -> dict:
        """Secure Vault capture flow entrypoint: returns the capture URL and
        opaque capture id. The secret is entered on the vault capture page
        (server-side); this client never sees or stores the secret value."""
        capture_id = self.registry.vault.create_capture(
            tenant_id=tenant_id, provider=provider, purpose=purpose)
        return {
            "capture_id": capture_id,
            "capture_url": f"/vault/capture/{capture_id}",
            "note": "Enter the credential on the Secure Vault capture page. "
                    "This client never sees or stores the secret value.",
        }

    def connect_with_capture(self, *, tenant_id: str, provider: str,
                             capture_id: str, scopes: list[str],
                             account_label: str = "") -> dict:
        """Finish an api_key connect from a completed vault capture.
        Only the opaque capture id crosses the client; the vault resolves
        the credential reference server-side."""
        result = self.registry.connect(
            tenant_id=tenant_id, provider=provider, auth_kind="api_key",
            scopes=scopes, account_label=account_label, capture_id=capture_id)
        safe = _safe(result)
        assert_no_secrets(safe, label="connector connect response")
        return safe

    def status(self, *, tenant_id: str, connection_id: str) -> dict:
        return _safe(self.registry.status(tenant_id=tenant_id,
                                          connection_id=connection_id))

    def request_scope_expansion(self, *, tenant_id: str, connection_id: str,
                                scopes: list[str], reason: str) -> dict:
        return _safe(self.registry.request_scope_expansion(
            tenant_id=tenant_id, connection_id=connection_id,
            new_scopes=scopes))

    def disconnect(self, *, tenant_id: str, connection_id: str) -> dict:
        return _safe(self.registry.disconnect(tenant_id=tenant_id,
                                              connection_id=connection_id))


# ---------------------------------------------------------------------------
# Browser handoff
# ---------------------------------------------------------------------------
class BrowserClient:
    """Browser handoff surface: start a session, observe, and surface the
    operator card; when a challenge needs the user, the handoff URL routes
    them to resolve it out-of-band (the agent never solves challenges)."""

    def __init__(self, operator):
        self.operator = operator

    def start_session(self, *, tenant_id: str = "tenant_demo",
                      url: str = "") -> dict:
        session_id = self.operator.start_session(tenant_id=tenant_id)
        attached = self.operator.attach_session(session_id)
        return {"session_id": session_id,
                "handoff_url": f"/browser/sessions/{session_id}",
                "url": attached.get("url", url),
                "state": attached.get("state", "active")}

    def observe(self, session_id: str) -> dict:
        return _safe(self.operator.observe(session_id))

    def mark_challenge_resolved(self, session_id: str,
                               *, by: str = "user") -> dict:
        return _safe(self.operator.mark_challenge_resolved(
            session_id, by=by))

    def close_session(self, session_id: str) -> dict:
        return _safe(self.operator.close_session(session_id))


# ---------------------------------------------------------------------------
# Usage / cost / privacy controls
# ---------------------------------------------------------------------------
class UsageClient:
    """Usage, cost, and privacy controls surface."""

    def __init__(self, production_services=None, tenant_id: str = "tenant_demo"):
        self.services = production_services
        self.tenant_id = tenant_id

    def summary(self) -> dict:
        if self.services is None:
            return {"usage": {}, "quotas": {},
                    "note": "no production services bound"}
        quotas = self.services.quotas.status(self.tenant_id) \
            if hasattr(self.services, "quotas") else {}
        metrics = self.services.metrics.snapshot() \
            if hasattr(self.services, "metrics") else {}
        return {"usage": _safe(metrics), "quotas": _safe(quotas)}

    def export_data(self) -> dict:
        """Portable export of the tenant's production stores (privacy)."""
        if self.services is None:
            raise RuntimeError("no production services bound")
        snap = self.services.backup.snapshot_tenant(self.tenant_id, [])
        return _safe({"tenant_id": self.tenant_id,
                      "snapshot_id": snap.snapshot_id,
                      "stores": snap.stores, "sha256": snap.sha256})
