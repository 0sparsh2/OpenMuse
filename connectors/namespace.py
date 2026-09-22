"""connector.* tools — Phase 6 authenticated service access.

  connector.connect        (R2, local_write) — begin a connection ceremony
                           (OAuth PKCE or Secure-Vault API-key capture).
  connector.complete_oauth (R2, local_write) — finish a PKCE ceremony.
  connector.status         (R0, none)        — connection health + declared
                           capabilities. Never returns credential material.
  connector.disconnect     (R2, local_write) — revoke + delete vault material.
  connector.<name>.<op>    (per manifest)    — one declared operation per
                           registered connector, e.g. connector.github.star.

Security posture:
  - Tools receive connection ids only. The registry resolves the vault
    reference into a short-lived handle scoped to one call.
  - External writes (R3+) execute only under a valid bound approval; the
    grant is single-use and consumed here, mirroring browser.commit.
  - Provider errors are data: ConnectorError/ ProviderError become a
    structured {ok:false} result, never a traceback. A terminal rate limit
    stops the provider for the run (registry-enforced).
"""
from __future__ import annotations

from connectors.models import ConnectorError, ProviderError
from connectors.registry import ConnectorRegistry
from tools.executor import argument_hash, canonicalize
from tools.registry import ToolDefinition, ToolRegistry


def _error_result(exc: Exception) -> dict:
    code = getattr(exc, "code", type(exc).__name__)
    safe = getattr(exc, "safe_message", str(exc))
    return {"ok": False, "error_code": code, "safe_message": safe}


def _check_grant_binding(grant, approvals, *, tool_name: str,
                         tool_version: str, args: dict) -> bool:
    if grant is None or approvals is None:
        return False
    if grant.tool_name != tool_name or grant.tool_version != tool_version:
        return False
    return grant.argument_hash == argument_hash(
        tool_name, tool_version, canonicalize(args))


def register(registry: ToolRegistry, connectors: ConnectorRegistry,
             approvals=None) -> None:
    registry.register_namespace(
        "connector",
        "Authenticated third-party services (Phase 6). Connect once, then "
        "call declared operations with a connection id. Credentials never "
        "appear in arguments, results, or logs.")

    # -- connection lifecycle -------------------------------------------------
    def connect(ctx, args):
        try:
            out = connectors.connect(
                tenant_id=ctx.tenant_id, provider=args["provider"],
                auth_kind=args["auth_kind"], scopes=list(args.get("scopes", [])),
                account_label=args.get("account_label", ""),
                capture_id=args.get("capture_id"))
        except (ConnectorError, ValueError, KeyError, PermissionError) as exc:
            return _error_result(exc)
        return {"ok": True, **out}

    registry.register(ToolDefinition(
        name="connector.connect", version="1.0.0",
        description=(
            "Begin connecting a third-party provider. auth_kind 'oauth_pkce' "
            "returns an authorization_url + state for the user to complete; "
            "'api_key' consumes a completed Secure Vault capture (capture_id) "
            "— the raw key NEVER passes through tool arguments."),
        input_schema={"type": "object",
                      "properties": {
                          "provider": {"type": "string"},
                          "auth_kind": {"type": "string",
                                       "enum": ["oauth_pkce", "api_key"]},
                          "scopes": {"type": "array",
                                     "items": {"type": "string"}},
                          "account_label": {"type": "string"},
                          "capture_id": {"type": "string",
                                        "description": "Completed vault capture "
                                                       "slot (api_key only)."}},
                      "required": ["provider", "auth_kind", "scopes"],
                      "additionalProperties": False},
        output_schema={"type": "object"},
        capabilities=["connector.manage"], side_effect="local_write",
        idempotency="keyed", default_timeout_ms=30_000, execute=connect,
    ))

    def complete_oauth(ctx, args):
        try:
            out = connectors.complete_authorization(
                tenant_id=ctx.tenant_id, state=args["state"], code=args["code"])
        except (ConnectorError, ValueError, KeyError, PermissionError) as exc:
            return _error_result(exc)
        return {"ok": True, **out}

    registry.register(ToolDefinition(
        name="connector.complete_oauth", version="1.0.0",
        description=(
            "Finish an OAuth PKCE ceremony after the user authorizes in "
            "their browser. Exchanges the code server-side and vaults the "
            "token; returns only the connection id and status."),
        input_schema={"type": "object",
                      "properties": {"state": {"type": "string"},
                                     "code": {"type": "string"}},
                      "required": ["state", "code"],
                      "additionalProperties": False},
        output_schema={"type": "object"},
        capabilities=["connector.manage"], side_effect="local_write",
        idempotency="keyed", default_timeout_ms=30_000,
        execute=complete_oauth,
    ))

    def status(ctx, args):
        try:
            view = connectors.status(tenant_id=ctx.tenant_id,
                                     connection_id=args["connection_id"])
        except (ConnectorError, KeyError) as exc:
            return _error_result(exc)
        return {"ok": True, **view}

    registry.register(ToolDefinition(
        name="connector.status", version="1.0.0",
        description=(
            "Connection health, granted scopes, and declared operations. "
            "Never returns credential material."),
        input_schema={"type": "object",
                      "properties": {"connection_id": {"type": "string"}},
                      "required": ["connection_id"],
                      "additionalProperties": False},
        output_schema={"type": "object"},
        capabilities=["connector.read"], side_effect="none",
        idempotency="pure", default_timeout_ms=5_000, execute=status,
    ))

    def disconnect(ctx, args):
        try:
            out = connectors.disconnect(tenant_id=ctx.tenant_id,
                                        connection_id=args["connection_id"])
        except (ConnectorError, KeyError) as exc:
            return _error_result(exc)
        return {"ok": True, **out}

    registry.register(ToolDefinition(
        name="connector.disconnect", version="1.0.0",
        description=(
            "Revoke the provider tokens where supported and delete the vault "
            "material. Later calls with this connection id fail closed."),
        input_schema={"type": "object",
                      "properties": {"connection_id": {"type": "string"}},
                      "required": ["connection_id"],
                      "additionalProperties": False},
        output_schema={"type": "object"},
        capabilities=["connector.manage"], side_effect="local_write",
        idempotency="keyed", default_timeout_ms=30_000,
        execute=disconnect,
    ))

    # -- one tool per declared operation --------------------------------------
    for provider, adapter in connectors._adapters.items():
        for opdef in adapter.manifest.operations:
            tool_name = f"connector.{provider}.{opdef.name}"

            def make_executor(op_name, opdef, tool_name):
                def execute_op(ctx, args):
                    call_args = {k: v for k, v in args.items()
                                 if k != "connection_id"}
                    # External writes execute only under a valid bound grant;
                    # the grant is single-use and consumed here.
                    if opdef.side_effect == "external_write":
                        grant = None
                        if approvals is not None and ctx.approval_grant_id:
                            grant = approvals.grants.get(ctx.approval_grant_id)
                        if not _check_grant_binding(
                                grant, approvals, tool_name=tool_name,
                                tool_version="1.0.0", args=args):
                            return {
                                "ok": False, "error_code": "APPROVAL_REQUIRED",
                                "safe_message": (
                                    f"{tool_name} is an external write and "
                                    "needs a valid bound approval."),
                            }
                        approvals.consume(grant)
                    try:
                        out = connectors.call(
                            run_id=ctx.run_id, tenant_id=ctx.tenant_id,
                            connection_id=args["connection_id"],
                            op=op_name, args=call_args)
                    except (ConnectorError, ProviderError) as exc:
                        return _error_result(exc)
                    return {"ok": True, **out}
                return execute_op

            properties = {"connection_id": {
                "type": "string",
                "description": "Connection from connector.connect."}}
            properties.update(
                (opdef.input_schema.get("properties") or {}))
            required = ["connection_id"] + list(
                opdef.input_schema.get("required", []))
            registry.register(ToolDefinition(
                name=tool_name, version="1.0.0",
                description=(opdef.description +
                             " Declared risk class and required scopes are "
                             "enforced by the connector registry."),
                input_schema={"type": "object", "properties": properties,
                              "required": required,
                              "additionalProperties": False},
                output_schema={"type": "object"},
                capabilities=[f"connector.{provider}.{opdef.name}"],
                side_effect=opdef.side_effect,
                idempotency=opdef.idempotency,
                default_timeout_ms=30_000,
                execute=make_executor(opdef.name, opdef, tool_name),
            ))
