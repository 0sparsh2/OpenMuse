"""production.* tools — Phase 8 operational surface.

  production.quota_status  (R1) — read a tenant's quota usage vs budget.
  production.backup_now     (R2) — snapshot a tenant across all stores.
  production.request_export (R2) — write a portable export bundle for a tenant.
  production.restore        (R3) — restore a tenant from a snapshot (overwrites
                           live state; measured against RTO/RPO objectives).
  production.delete_tenant  (R4) — irreversibly delete a tenant across every
                           store, then return the residual audit. Requires a
                           bound approval AND confirm == tenant_id.
  production.channel_send   (R2) — send a message over a messaging channel
                           (external_write; may carry an approval deep link).

The namespace is registered with a ProductionServices bundle; the tools are
thin wrappers — authorization stays in the deterministic policy engine.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from tools.registry import ToolDefinition, ToolRegistry

from ._store import atomic_write_json, utcnow
from .channels import ChannelAdapter, DeepLinkService
from .dr import BackupService, TenantDeletionService
from .metrics import Metrics
from .quotas import QuotaManager


@dataclass
class ProductionServices:
    quotas: QuotaManager
    backup: BackupService
    deletion: TenantDeletionService
    deeplinks: DeepLinkService
    metrics: Metrics
    channels: dict[str, ChannelAdapter] = field(default_factory=dict)
    # participants_for(tenant_id) -> list of BackupParticipant
    participants_for: object = None
    exports_root: str = ""


def register(registry: ToolRegistry, services: ProductionServices) -> None:
    registry.register_namespace(
        "production", "Production operations: quotas, backup/restore, "
                      "export/delete, messaging (Phase 8).")

    def _participants(tenant_id: str) -> list:
        if services.participants_for is None:
            raise RuntimeError("no participants configured")
        return services.participants_for(tenant_id)

    # -- quota_status (R1) -------------------------------------------------
    def quota_status(ctx, args):
        tenant_id = args.get("tenant_id") or ctx.tenant_id
        return services.quotas.status(tenant_id)

    registry.register(ToolDefinition(
        name="production.quota_status", version="1.0.0",
        description="Read a tenant's quota usage (calls, tokens, spend) "
                    "against budget for the current window.",
        input_schema={"type": "object",
                      "properties": {"tenant_id": {"type": "string"}},
                      "additionalProperties": False},
        output_schema={"type": "object"},
        capabilities=["production.read"], side_effect="none",
        idempotency="pure", default_timeout_ms=5_000, execute=quota_status,
    ))

    # -- backup_now (R2) ---------------------------------------------------
    def backup_now(ctx, args):
        tenant_id = args.get("tenant_id") or ctx.tenant_id
        snap = services.backup.snapshot_tenant(
            tenant_id, _participants(tenant_id))
        services.metrics.incr(tenant_id, "production.backup")
        return {"snapshot_id": snap.snapshot_id, "tenant_id": tenant_id,
                "created_at": snap.created_at, "stores": snap.stores,
                "sha256": snap.sha256}

    registry.register(ToolDefinition(
        name="production.backup_now", version="1.0.0",
        description="Snapshot a tenant's data across all stores into a "
                    "checksummed backup bundle.",
        input_schema={"type": "object",
                      "properties": {"tenant_id": {"type": "string"}},
                      "additionalProperties": False},
        output_schema={"type": "object"},
        capabilities=["production.operate"], side_effect="local_write",
        idempotency="keyed", default_timeout_ms=30_000, execute=backup_now,
    ))

    # -- request_export (R2) -----------------------------------------------
    def request_export(ctx, args):
        tenant_id = args.get("tenant_id") or ctx.tenant_id
        snap = services.backup.snapshot_tenant(
            tenant_id, _participants(tenant_id))
        export = {"exported_at": utcnow(), "tenant_id": tenant_id,
                  "snapshot_id": snap.snapshot_id,
                  "stores": snap.stores, "sha256": snap.sha256,
                  "note": "Portable export: re-import with production.restore"}
        path = os.path.join(services.exports_root, tenant_id,
                            f"export-{snap.snapshot_id}.json")
        atomic_write_json(path, export)
        services.metrics.incr(tenant_id, "production.export")
        return {"tenant_id": tenant_id, "path": path,
                "snapshot_id": snap.snapshot_id, "sha256": snap.sha256}

    registry.register(ToolDefinition(
        name="production.request_export", version="1.0.0",
        description="Write a portable export bundle of a tenant's data "
                    "(snapshot + manifest).",
        input_schema={"type": "object",
                      "properties": {"tenant_id": {"type": "string"}},
                      "additionalProperties": False},
        output_schema={"type": "object"},
        capabilities=["production.operate"], side_effect="local_write",
        idempotency="keyed", default_timeout_ms=30_000,
        execute=request_export,
    ))

    # -- restore (R3) ------------------------------------------------------
    def restore(ctx, args):
        tenant_id = args.get("tenant_id") or ctx.tenant_id
        report = services.backup.restore_tenant(
            tenant_id, args["snapshot_id"], _participants(tenant_id))
        services.metrics.incr(tenant_id, "production.restore")
        return report.to_dict()

    registry.register(ToolDefinition(
        name="production.restore", version="1.0.0",
        description="Restore a tenant from a backup snapshot, overwriting "
                    "live state. Reports RTO/RPO against objectives.",
        input_schema={"type": "object",
                      "properties": {"tenant_id": {"type": "string"},
                                     "snapshot_id": {"type": "string"}},
                      "required": ["snapshot_id"],
                      "additionalProperties": False},
        output_schema={"type": "object"},
        capabilities=["production.operate"], side_effect="local_write",
        idempotency="keyed", default_timeout_ms=60_000, execute=restore,
    ))

    # -- delete_tenant (R4, destructive) ------------------------------------
    def delete_tenant(ctx, args):
        tenant_id = args.get("tenant_id") or ctx.tenant_id
        if args.get("confirm") != tenant_id:
            raise ValueError("refusing tenant deletion: confirm must equal "
                             "tenant_id")
        audit = services.deletion.delete_tenant(
            tenant_id, _participants(tenant_id))
        services.metrics.incr(tenant_id, "production.delete_tenant")
        return audit.to_dict()

    registry.register(ToolDefinition(
        name="production.delete_tenant", version="1.0.0",
        description="IRREVERSIBLY delete a tenant across every store "
                    "(database, objects, vectors, vault, browser profiles, "
                    "schedules, queue, memory, backups), then return the "
                    "residual audit. Requires a bound approval and "
                    "confirm == tenant_id.",
        input_schema={"type": "object",
                      "properties": {"tenant_id": {"type": "string"},
                                     "confirm": {"type": "string",
                                                 "description": "Must equal tenant_id"}},
                      "required": ["tenant_id", "confirm"],
                      "additionalProperties": False},
        output_schema={"type": "object"},
        capabilities=["production.destroy"], side_effect="destructive",
        idempotency="unsafe_retry", default_timeout_ms=60_000,
        execute=delete_tenant,
    ))

    # -- channel_send (R2, external_write) -----------------------------------
    def channel_send(ctx, args):
        adapter = services.channels.get(args["channel"])
        if adapter is None:
            raise ValueError(f"unknown channel {args['channel']!r}")
        mid = adapter.send(args["recipient"], args["text"],
                           deep_link=args.get("deep_link", ""))
        services.metrics.incr(ctx.tenant_id, "production.channel_send")
        return {"channel": args["channel"], "message_id": mid}

    registry.register(ToolDefinition(
        name="production.channel_send", version="1.0.0",
        description="Send a message over a messaging channel. Optionally "
                    "attach an approval deep link.",
        input_schema={"type": "object",
                      "properties": {
                          "channel": {"type": "string"},
                          "recipient": {"type": "string", "maxLength": 200},
                          "text": {"type": "string", "maxLength": 4000},
                          "deep_link": {"type": "string", "maxLength": 500},
                      },
                      "required": ["channel", "recipient", "text"],
                      "additionalProperties": False},
        output_schema={"type": "object"},
        capabilities=["messaging.send"], side_effect="external_write",
        idempotency="keyed", default_timeout_ms=10_000,
        execute=channel_send,
    ))
