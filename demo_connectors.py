"""Phase 6 verification: connectors and secure credential use.

Proves, fully offline and deterministic:
  A. connector registration: manifest declared; unknown/duplicate rejected
  B. OAuth PKCE connect: ceremony returns url+state, no secrets; status shows
     capabilities, never credential material
  C. API-key connect via Secure Vault capture: the raw key never passes
     through tool arguments; captures are single-use
  D. read op under policy: connector.github.get_repo -> R0 -> ALLOW
  E. scope enforcement: op outside granted scopes -> SCOPE_NOT_GRANTED
  F. external write: star without approval -> DENY; bound approval -> ALLOW;
     tampered args -> DENY; grant is single-use
  G. terminal rate limit -> hard stop for (run, account); other runs unaffected
  H. secret-leak guard: provider echoing the credential -> blocked, redacted
  I. disconnect: vault material deleted; later calls fail closed
  J. one-time-code path: agent sees only success/failure, never the code
  K. scope expansion requires a new consent ceremony
  L. connector.* tools through the registry + policy YAML mappings
  M. tenant isolation on connections
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import replace

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from connectors import (
    ConnectorRegistry, MemoryVault, OAuthFlow, OneTimeCodeService,
)
from connectors.github import GitHubConnector, mock_github_transport
from connectors.models import ConnectorError, ProviderError, ProviderErrorCode
from connectors.namespace import register as register_connectors
from connectors.rest import HTTPResponse
from observability.events import EventLog
from policy import ApprovalService, AutoApproveDecider, PolicyEngine
from policy.engine import ALLOW, ASK, DENY, PolicyInput
from tools import (
    ExecutionContext, ToolRegistry, execute_batch, prevalidate,
)

CHECKS: list[tuple[str, bool, str]] = []
CALL_SEQ = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


def build():
    log = EventLog("run_connectors_demo")
    vault = MemoryVault()
    oauth = OAuthFlow()
    registry = ConnectorRegistry(vault=vault, oauth=oauth, event_log=log)
    transport = mock_github_transport()
    registry.register_connector(GitHubConnector(transport))
    tools = ToolRegistry()
    approvals = ApprovalService()
    register_connectors(tools, registry, approvals=approvals)
    policy = PolicyEngine(os.path.join(ROOT, "policies", "tool-capabilities.yaml"))
    decider = AutoApproveDecider(allow_risks={"R1", "R2"})
    return tools, registry, vault, transport, policy, approvals, decider, log


def ctx_for(log, tenant="tenant_demo", run="run_connectors_demo"):
    return ExecutionContext(run_id=run, tenant_id=tenant,
                            workspace_root="/tmp",
                            event_log=log)


def call_tool(tools, policy, approvals, decider, ctx, tool_name, args):
    """Mirror the tool-call lifecycle. Returns (decision, envelope|None)."""
    global CALL_SEQ
    CALL_SEQ += 1
    call = prevalidate(tools, f"c{CALL_SEQ}", tool_name, args)
    if call.error:
        return ("PREVALIDATION_FAILED", {"error": call.error})
    risk = policy.risk_of(tool_name)
    grant = approvals.find_valid_grant(
        run_id=ctx.run_id, tool_name=tool_name,
        tool_version=call.tool.version, argument_hash=call.argument_hash)
    decision = policy.evaluate(PolicyInput(
        tool_name=tool_name, tool_version=call.tool.version,
        argument_hash=call.argument_hash, risk=risk,
        capabilities=list(call.tool.capabilities),
        side_effect=policy.side_effect_of(tool_name),
        argument_summary={"args": str(call.arguments)[:300]},
        has_valid_approval=grant is not None))
    if decision.decision == DENY:
        return (DENY, None)
    if decision.decision == ASK:
        req = approvals.create_request(
            run_id=ctx.run_id, tenant_id=ctx.tenant_id,
            tool_name=tool_name, tool_version=call.tool.version,
            argument_hash=call.argument_hash,
            bind_fields={"summary": str(call.arguments)[:200]},
            risk=risk, template=decision.approval_template)
        verdict = decider.decide(req)
        grant = approvals.resolve(req.id, verdict,
                                  decided_by=type(decider).__name__)
        if grant is None:
            return ("APPROVAL_DENIED", None)
        ctx = replace(ctx, approval_grant_id=grant.id)
    (envelope,) = execute_batch(tools, ctx, [call])
    return (decision.decision, envelope)


def model_view(envelope):
    mv = envelope.get("model_view")
    return mv if isinstance(mv, dict) else {}


def serialized(d) -> str:
    return json.dumps(d, ensure_ascii=False, default=str)


def main() -> int:
    tools, registry, vault, transport, policy, approvals, decider, log = build()
    ctx = ctx_for(log)
    TENANT = "tenant_demo"
    RUN = "run_connectors_demo"

    # -- A. registration ------------------------------------------------------
    try:
        registry.register_connector(GitHubConnector(transport))
        check("duplicate connector rejected", False)
    except ValueError:
        check("duplicate connector rejected", True)
    try:
        registry.manifest("nosuch")
        check("unknown provider rejected", False)
    except ConnectorError as e:
        check("unknown provider rejected", e.code == "UNKNOWN_PROVIDER", e.code)
    check("connector namespace registered",
          "connector" in tools.namespace_names())
    check("github op tools registered",
          all(t in tools.tool_names() for t in
              ("connector.github.get_repo", "connector.github.star",
               "connector.github.unstar")))

    # -- B. OAuth PKCE connect -------------------------------------------------
    decision, env = call_tool(
        tools, policy, approvals, decider, ctx, "connector.connect",
        {"provider": "github", "auth_kind": "oauth_pkce",
         "scopes": ["repo.read", "repo.write"], "account_label": "personal"})
    started = model_view(env)
    check("oauth connect starts ceremony (ASK->approved->executed)",
          decision in ("ASK", ALLOW) and started.get("ok") is True, decision)
    check("ceremony returns url+state, no secret",
          started.get("authorization_url", "").startswith("mock://oauth/")
          and started.get("state", "").startswith("st_")
          and "mock-oauth" not in serialized(started), serialized(started)[:120])
    con_id = started["connection_id"]
    state = started["state"]

    decision, env = call_tool(
        tools, policy, approvals, decider, ctx, "connector.status",
        {"connection_id": con_id})
    pending = model_view(env)
    check("pending status has no credential material",
          pending.get("status") == "pending"
          and "credential_ref" not in pending
          and "vault://" not in serialized(pending), serialized(pending)[:150])

    decision, env = call_tool(
        tools, policy, approvals, decider, ctx, "connector.complete_oauth",
        {"state": state, "code": "mock-code-user-pasted"})
    done = model_view(env)
    check("oauth completion connects", decision in ("ASK", ALLOW)
          and done.get("status") == "healthy", decision)

    decision, env = call_tool(
        tools, policy, approvals, decider, ctx, "connector.status",
        {"connection_id": con_id})
    st = model_view(env)
    check("status reports scopes+ops, never secrets",
          st.get("status") == "healthy"
          and set(st.get("granted_scopes", [])) == {"repo.read", "repo.write"}
          and "credential_ref" not in st
          and "mock-oauth" not in serialized(st)
          and "mock-refresh" not in serialized(st)
          and any(o["name"] == "star" for o in st.get("operations", [])),
          serialized(st)[:150])

    # -- C. API-key capture flow ------------------------------------------------
    cap = vault.create_capture(tenant_id=TENANT, provider="github",
                               purpose="api-key connect")
    vault.complete_capture(cap, "sk-test-fake-key-1234567890")  # USER-side page
    decision, env = call_tool(
        tools, policy, approvals, decider, ctx, "connector.connect",
        {"provider": "github", "auth_kind": "api_key",
         "scopes": ["repo.read"], "capture_id": cap})
    keyed = model_view(env)
    keyed_id = keyed.get("connection_id", "")
    check("api-key connect via capture (key never in tool args)",
          decision in ("ASK", ALLOW) and keyed.get("ok") is True
          and "sk-test-fake" not in serialized(keyed), decision)
    decision2, env2 = call_tool(
        tools, policy, approvals, decider, ctx, "connector.connect",
        {"provider": "github", "auth_kind": "api_key",
         "scopes": ["repo.read"], "capture_id": cap})
    check("capture slot is single-use",
          model_view(env2).get("error_code") in ("CAPTURE_REQUIRED",)
          or model_view(env2).get("ok") is False, decision2)

    # -- D. read op under policy -------------------------------------------------
    decision, env = call_tool(
        tools, policy, approvals, decider, ctx, "connector.github.get_repo",
        {"connection_id": con_id, "owner": "0sparsh2", "repo": "OpenMuse"})
    repo = model_view(env)
    check("get_repo R0 -> ALLOW",
          decision == ALLOW and repo.get("full_name") == "0sparsh2/OpenMuse",
          decision)
    check("read result carries no credential",
          "mock-oauth" not in serialized(repo)
          and "Bearer" not in serialized(repo), serialized(repo)[:120])

    # -- E. scope enforcement ------------------------------------------------------
    decision, env = call_tool(
        tools, policy, approvals, decider, ctx, "connector.github.star",
        {"connection_id": keyed_id, "owner": "0sparsh2", "repo": "OpenMuse"})
    check("star on read-only connection: policy DENYs first (R3, no grant)",
          decision == DENY, decision)
    # NOTE: star is R3 -> policy DENYs without a bound approval first; the
    # scope check happens inside the registry once approved. Prove the scope
    # denial at the registry level:
    try:
        registry.call(run_id=RUN, tenant_id=TENANT, connection_id=keyed_id,
                      op="star", args={"owner": "0sparsh2", "repo": "OpenMuse"})
        check("star on read-only connection -> SCOPE_NOT_GRANTED", False)
    except ConnectorError as e:
        check("star on read-only connection -> SCOPE_NOT_GRANTED",
              e.code == "SCOPE_NOT_GRANTED", e.code)

    # -- F. external write approval dance -------------------------------------------
    star_args = {"connection_id": con_id, "owner": "0sparsh2", "repo": "OpenMuse"}
    decision, _ = call_tool(tools, policy, approvals, decider, ctx,
                            "connector.github.star", star_args)
    check("star without approval -> DENY (R3)", decision == DENY, decision)

    # Approve the exact call (bound to argument hash).
    global CALL_SEQ
    CALL_SEQ += 1
    pv = prevalidate(tools, f"c{CALL_SEQ}", "connector.github.star", star_args)
    req = approvals.create_request(
        run_id=RUN, tenant_id=TENANT, tool_name="connector.github.star",
        tool_version=pv.tool.version, argument_hash=pv.argument_hash,
        bind_fields={"destination": "github:0sparsh2/OpenMuse",
                     "effect": "star", "content_hash": "none:read-write"},
        risk="R3", template="external_write_v1")
    grant = approvals.resolve(req.id, "approved", decided_by="user")
    ctx_g = replace(ctx, approval_grant_id=grant.id)
    decision, env = call_tool(tools, policy, approvals, decider, ctx_g,
                              "connector.github.star", star_args)
    out = model_view(env)
    check("star with bound approval -> ALLOW + executes",
          decision == ALLOW and out.get("starred") is True, decision)

    # Grant is single-use: reuse without a fresh grant -> DENY.
    decision, _ = call_tool(tools, policy, approvals, decider, ctx_g,
                            "connector.github.star", star_args)
    check("consumed grant cannot be reused", decision == DENY, decision)

    # Tampered args invalidate the approval.
    CALL_SEQ += 1
    pv2 = prevalidate(tools, f"c{CALL_SEQ}", "connector.github.star", star_args)
    req2 = approvals.create_request(
        run_id=RUN, tenant_id=TENANT, tool_name="connector.github.star",
        tool_version=pv2.tool.version, argument_hash=pv2.argument_hash,
        bind_fields={"destination": "github:0sparsh2/OpenMuse", "effect": "star"},
        risk="R3", template="external_write_v1")
    grant2 = approvals.resolve(req2.id, "approved", decided_by="user")
    ctx_g2 = replace(ctx, approval_grant_id=grant2.id)
    evil_args = {"connection_id": con_id, "owner": "someone", "repo": "elsewhere"}
    decision, _ = call_tool(tools, policy, approvals, decider, ctx_g2,
                            "connector.github.star", evil_args)
    check("mutated args void the approval", decision == DENY, decision)

    # -- G. terminal rate-limit hard stop ---------------------------------------------
    transport.add_route(
        "GET", "/repos/0sparsh2/OpenMuse",
        lambda p, b, h: HTTPResponse(429, {"message": "API rate limit exceeded"},
                                     {"x-ratelimit-remaining": "0"}))
    try:
        registry.call(run_id=RUN, tenant_id=TENANT, connection_id=con_id,
                      op="get_repo", args={"owner": "0sparsh2", "repo": "OpenMuse"})
        check("terminal rate limit classified", False)
    except ProviderError as e:
        check("terminal rate limit classified",
              e.code == ProviderErrorCode.RATE_LIMIT_TERMINAL, e.code)
    try:
        registry.call(run_id=RUN, tenant_id=TENANT, connection_id=con_id,
                      op="get_repo", args={"owner": "0sparsh2", "repo": "OpenMuse"})
        check("hard stop blocks further calls this run", False)
    except ConnectorError as e:
        check("hard stop blocks further calls this run",
              e.code == "PROVIDER_HARD_STOP", e.code)
    check("transport saw no retry after terminal limit",
          sum(1 for c in transport.calls if c["url"].endswith("/repos/0sparsh2/OpenMuse")
              and c["method"] == "GET") == 2,
          str(len(transport.calls)))
    # A different run is unaffected (stop is per run).
    transport.add_route(
        "GET", "/repos/0sparsh2/OpenMuse",
        lambda p, b, h: HTTPResponse(200, {"full_name": "0sparsh2/OpenMuse"}))
    out = registry.call(run_id="run_other", tenant_id=TENANT,
                        connection_id=con_id, op="get_repo",
                        args={"owner": "0sparsh2", "repo": "OpenMuse"})
    check("other runs unaffected by the stop",
          out.get("full_name") == "0sparsh2/OpenMuse")

    # -- H. secret-leak guard ------------------------------------------------------------
    transport.add_route(
        "GET", "/repos/0sparsh2/OpenMuse",
        lambda p, b, h: HTTPResponse(200, {"echo": h.get("Authorization")}))
    try:
        registry.call(run_id="run_leak", tenant_id=TENANT, connection_id=con_id,
                      op="get_repo", args={"owner": "0sparsh2", "repo": "OpenMuse"})
        check("credential echo blocked", False)
    except ConnectorError as e:
        check("credential echo blocked",
              e.code == "SECRET_LEAK_BLOCKED", e.code)
    check("leak emitted an observability event",
          any(ev.type == "connector.secret_leak_blocked" for ev in log.events))
    transport.add_route(
        "GET", "/repos/0sparsh2/OpenMuse",
        lambda p, b, h: HTTPResponse(200, {"full_name": "0sparsh2/OpenMuse"}))

    # -- I. disconnect ---------------------------------------------------------------------
    ref_before = registry._connections[con_id].credential_ref
    check("vault holds material before disconnect", vault.has(ref_before))
    decision, env = call_tool(tools, policy, approvals, decider, ctx,
                              "connector.disconnect", {"connection_id": con_id})
    check("disconnect revokes", decision in ("ASK", ALLOW)
          and model_view(env).get("status") == "revoked", decision)
    check("vault material deleted on disconnect",
          not vault.has(ref_before)
          and registry._refresh_refs.get(con_id) is None)
    try:
        registry.call(run_id="run_after", tenant_id=TENANT, connection_id=con_id,
                      op="get_repo", args={"owner": "0sparsh2", "repo": "OpenMuse"})
        check("calls after disconnect fail closed", False)
    except ConnectorError as e:
        check("calls after disconnect fail closed",
              e.code == "CONNECTION_NOT_HEALTHY", e.code)

    # -- J. one-time-code protected path ------------------------------------------------------
    otp = OneTimeCodeService()
    user_device: dict = {}
    issued = otp.issue(keyed_id, deliver=lambda code: user_device.update(code=code))
    check("issue returns no code to the agent",
          set(issued.keys()) == {"code_id", "connection_id", "status"}
          and user_device.get("code") not in serialized(issued),
          serialized(issued))
    res = otp.fill(issued["code_id"], user_device["code"], connection_id=keyed_id)
    check("correct code -> success only", res == {"ok": True}, str(res))
    issued2 = otp.issue(keyed_id, deliver=lambda code: None)
    res2 = otp.fill(issued2["code_id"], "000000", connection_id=keyed_id)
    check("wrong code -> failure only",
          res2 == {"ok": False, "reason": "incorrect_code"}, str(res2))

    # -- K. scope expansion needs a new ceremony --------------------------------------------------
    exp = registry.request_scope_expansion(
        tenant_id=TENANT, connection_id=keyed_id, new_scopes=["repo.write"])
    check("expansion starts a new ceremony",
          exp.get("state", "").startswith("st_")
          and "authorization_url" in exp, str(exp)[:100])
    before = registry.status(tenant_id=TENANT, connection_id=keyed_id)
    check("scopes unchanged before the new ceremony completes",
          before["granted_scopes"] == ["repo.read"], str(before["granted_scopes"]))
    done_exp = registry.complete_authorization(
        tenant_id=TENANT, state=exp["state"], code="mock-code-expansion")
    after = registry.status(tenant_id=TENANT, connection_id=keyed_id)
    check("expanded scopes granted after ceremony",
          set(after["granted_scopes"]) == {"repo.read", "repo.write"},
          str(after["granted_scopes"]))
    # The expanded connection can now star (registry level).
    out = registry.call(run_id="run_exp", tenant_id=TENANT,
                        connection_id=keyed_id, op="star",
                        args={"owner": "0sparsh2", "repo": "OpenMuse"})
    check("star works after scope expansion", out.get("starred") is True)

    # -- L. policy mappings ------------------------------------------------------------------------
    for tool_name, want in [
        ("connector.connect", "R2"), ("connector.complete_oauth", "R2"),
        ("connector.status", "R0"), ("connector.disconnect", "R2"),
        ("connector.github.get_repo", "R0"), ("connector.github.star", "R3"),
        ("connector.github.unstar", "R3"),
    ]:
        check(f"policy maps {tool_name} -> {want}",
              policy.risk_of(tool_name) == want, policy.risk_of(tool_name))
    check("unmapped connector tool fails closed",
          policy.risk_of("connector.github.nosuchop") == "R5")

    # -- M. tenant isolation ---------------------------------------------------------------------------
    try:
        registry.status(tenant_id="tenant_other", connection_id=keyed_id)
        check("cross-tenant status refused", False)
    except ConnectorError as e:
        check("cross-tenant status refused",
              e.code == "UNKNOWN_CONNECTION", e.code)
    try:
        registry.call(run_id="run_x", tenant_id="tenant_other",
                      connection_id=keyed_id, op="get_repo",
                      args={"owner": "0sparsh2", "repo": "OpenMuse"})
        check("cross-tenant call refused", False)
    except ConnectorError as e:
        check("cross-tenant call refused",
              e.code == "UNKNOWN_CONNECTION", e.code)

    # -- lifecycle events present -----------------------------------------------------------------------
    types = {ev.type for ev in log.events}
    for want in ("connector.registered", "connector.auth_started",
                 "connector.connected", "connector.call_started",
                 "connector.call_succeeded", "connector.hard_stop",
                 "connector.disconnected", "connector.scopes_expanded"):
        check(f"event emitted: {want}", want in types, sorted(types))

    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
