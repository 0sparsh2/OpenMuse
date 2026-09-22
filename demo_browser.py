"""Phase 4 verification: managed browser computer use.

Proves, fully offline and deterministic (mock page driver, no network):
  A. session -> observe -> navigate -> grounded click/type actions
  B. stale element ids rejected after navigation
  C. challenge detected -> session pauses -> hands to user -> user resolves
     -> session continues; no evasion (captcha click / unknown kinds denied)
  D. commit barrier: consequential click returns a proposal, never executes;
     confirm needs a fresh state-bound approval; single-use grants;
     tampered args denied; page change invalidates the proposal
  E. egress policy: non-mock navigation denied; closed action union
  F. conservative pacing enforced
  G. checkpoint + crash recovery without replaying commits
  H. persistent profile: a new operator instance re-attaches the session
  I. credential hygiene: password fill via opaque ref under approval;
     values never appear in observations/logs
  J. policy risk mapping: observe=R0, act=R2, confirm_commit=R4
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from agent.seams import BrowserOperator
from browser.namespace import register as register_browser, risk_for_action
from browser.operator import BrowserError, ManagedBrowserOperator
from observability.events import EventLog
from policy import ApprovalService, AutoApproveDecider, PolicyEngine
from policy.engine import ALLOW, ASK, DENY, PolicyInput
from tools import namespaces as ns  # noqa: F401  (package import)
from tools.executor import ExecutionContext, execute_batch, prevalidate
from tools.namespaces import (
    file_tools, math_tools, memory_tools, shell_tools, system_tools, web_tools,
)
from tools.registry import ToolRegistry

CHECKS: list[tuple[str, bool, str]] = []
CALL_SEQ = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" +
          (f" — {detail}" if detail and not cond else ""))


def build(profile_root: str, **op_kwargs):
    registry = ToolRegistry()
    loaded: set[str] = set()
    system_tools.register(registry, loaded_namespaces=loaded)
    math_tools.register(registry)
    file_tools.register(registry)
    shell_tools.register(registry)
    web_tools.register(registry)
    memory_tools.register(registry)
    operator = ManagedBrowserOperator(profile_root, **op_kwargs)
    approvals = ApprovalService()
    register_browser(registry, operator, approvals=approvals)
    policy = PolicyEngine(os.path.join(ROOT, "policies", "tool-capabilities.yaml"))
    decider = AutoApproveDecider(allow_risks={"R1", "R2"})
    log = EventLog("run_browser_demo")
    return registry, operator, policy, approvals, decider, log


def ctx_for(log, tenant="tenant_demo", run="run_browser_demo"):
    return ExecutionContext(run_id=run, tenant_id=tenant,
                            workspace_root=tempfile.gettempdir(),
                            event_log=log)


def call_tool(registry, policy, approvals, decider, ctx, tool_name, args,
              *, bind_fields=None, risk_override=None):
    """Mirror the tool-call lifecycle. Returns (decision, envelope|None)."""
    global CALL_SEQ
    CALL_SEQ += 1
    call = prevalidate(registry, f"c{CALL_SEQ}", tool_name, args)
    if call.error:
        return ("PREVALIDATION_FAILED", {"error": call.error})
    risk = risk_override or risk_for_action(policy, tool_name, call.arguments)
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
            bind_fields=bind_fields or {"summary": str(call.arguments)[:200]},
            risk=risk, template=decision.approval_template)
        verdict = decider.decide(req)
        grant = approvals.resolve(req.id, verdict,
                                  decided_by=type(decider).__name__)
        if grant is None:
            return ("APPROVAL_DENIED", None)
        ctx = ExecutionContext(run_id=ctx.run_id, tenant_id=ctx.tenant_id,
                               workspace_root=ctx.workspace_root,
                               event_log=ctx.event_log,
                               approval_grant_id=grant.id,
                               deadline_ms=ctx.deadline_ms)
    (envelope,) = execute_batch(registry, ctx, [call])
    return (decision.decision, envelope)


def model_view(envelope):
    mv = envelope.get("model_view")
    return mv if isinstance(mv, dict) else {}


def el_id(obs, name):
    for el in obs["interactive_elements"]:
        if el["name"] == name:
            return el["element_id"]
    raise AssertionError(f"no element named {name!r}")


def approve_commit(approvals, decider4, ctx, proposal_bind, args):
    """Create a fresh state-bound approval for a confirm_commit call."""
    global CALL_SEQ
    CALL_SEQ += 1
    call = prevalidate(registry_g, f"c{CALL_SEQ}", "browser.act", args)
    assert not call.error, call.error
    req = approvals.create_request(
        run_id=ctx.run_id, tenant_id=ctx.tenant_id,
        tool_name="browser.act", tool_version="1.0.0",
        argument_hash=call.argument_hash, bind_fields=proposal_bind,
        risk="R4", template="browser_commit_v1")
    verdict = decider4.decide(req)
    grant = approvals.resolve(req.id, verdict, decided_by="TestDecider")
    assert grant is not None, "R4 approval should be granted by test decider"
    return grant


def main() -> int:
    global registry_g
    profile_root = tempfile.mkdtemp(prefix="openmuse-phase4-")
    registry, operator, policy, approvals, decider, log = build(profile_root)
    registry_g = registry
    decider4 = AutoApproveDecider(allow_risks={"R1", "R2", "R4"})
    ctx = ctx_for(log)

    # -- A. session -> observe -> navigate -> grounded actions --------------
    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.start_session", {})
    sid = model_view(env)["session_id"]
    obs0 = model_view(env)["observation"]
    check("A1 session starts on mock home",
          d in (ALLOW, ASK) and obs0["url"] == "mock://news/", d)
    check("A2 observation carries grounded element ids",
          len(obs0["interactive_elements"]) >= 3
          and all(e["element_id"].startswith("el_1_")
                  for e in obs0["interactive_elements"]),
          json.dumps(obs0["interactive_elements"])[:120])

    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.act", {"session_id": sid,
                                       "action": {"kind": "navigate",
                                                  "url": "mock://shop/"}})
    obs = model_view(env)["observation"]
    check("A3 navigate to shop", obs["url"] == "mock://shop/"
          and obs["navigation_id"] == 2, obs["url"])

    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.act", {"session_id": sid,
                                       "action": {"kind": "click",
                                                  "element_id": el_id(obs, "Widget Pro — $129.99")}})
    obs = model_view(env)["observation"]
    check("A4 grounded click follows the link",
          obs["url"] == "mock://shop/product-1", obs["url"])

    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.act", {"session_id": sid,
                                       "action": {"kind": "click",
                                                  "element_id": el_id(obs, "Add to cart")}})
    mv = model_view(env)
    check("A5 add-to-cart is a reversible grounded action",
          mv.get("cart_added") == "Widget Pro"
          and mv["observation"]["cart"] == ["Widget Pro"],
          json.dumps(mv.get("observation", {}).get("cart")))

    # -- B. stale element ids ------------------------------------------------
    stale_id = obs0["interactive_elements"][0]["element_id"]  # el_1_* on nav 4
    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.act", {"session_id": sid,
                                       "action": {"kind": "click",
                                                  "element_id": stale_id}})
    mv = model_view(env)
    check("B1 stale element id rejected",
          mv.get("status") == "refused" and mv.get("code") == "STALE_ELEMENT",
          json.dumps(mv)[:160])

    # -- C. challenge: pause, hand to user, resume ---------------------------
    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.start_session", {})
    sid2 = model_view(env)["session_id"]
    call_tool(registry, policy, approvals, decider, ctx, "browser.act",
              {"session_id": sid2,
               "action": {"kind": "navigate",
                          "url": "mock://accounts/login"}})
    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.observe", {"session_id": sid2})
    login_obs = model_view(env)
    email_el = next(e["element_id"] for e in login_obs["interactive_elements"]
                    if e["name"] == "Email")
    pw_el = next(e["element_id"] for e in login_obs["interactive_elements"]
                 if e["name"] == "Password")
    signin_el = next(e["element_id"] for e in login_obs["interactive_elements"]
                     if e["name"] == "Sign in")

    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.act", {"session_id": sid2,
                                       "action": {"kind": "type",
                                                  "element_id": email_el,
                                                  "text": "user@example.com"}})
    check("C1 type into email field",
          model_view(env).get("typed_into") == "Email", d)

    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.act", {"session_id": sid2,
                                       "action": {"kind": "type",
                                                  "element_id": pw_el,
                                                  "text_ref": "vault://tenant/mock-password"}})
    mv = model_view(env)
    pw_field = next(f for form in mv["observation"]["forms"]
                    for f in form["fields"] if f["field_id"] == "password")
    trace = json.dumps(mv)
    check("C2 credential fill uses opaque ref under approval; value never surfaces",
          mv.get("credential") is True and pw_field.get("value_set") is True
          and "value" not in pw_field and "mock-password" not in trace
          and "s3cr3t" not in trace, json.dumps(pw_field))

    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.act", {"session_id": sid2,
                                       "action": {"kind": "click",
                                                  "element_id": signin_el}})
    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.observe", {"session_id": sid2})
    ch_obs = model_view(env)
    check("C3 challenge detected and session paused",
          ch_obs["challenges"] == ["captcha"]
          and ch_obs["session_state"] == "challenged", json.dumps(ch_obs["challenges"]))

    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.act", {"session_id": sid2,
                                       "action": {"kind": "click",
                                                  "element_id": ch_obs["interactive_elements"][0]["element_id"]}})
    mv = model_view(env)
    check("C4 acting while challenged hands to the user",
          mv.get("status") == "challenge_paused"
          and "handoff_to_user" in mv, json.dumps(mv)[:160])

    # Evasion attempts are denied, not executed.
    try:
        operator.act(sid2, {"kind": "solve_captcha"})
        evasion_denied = False
    except BrowserError as exc:
        evasion_denied = exc.code == "EVASION_PROHIBITED"
    check("C5 non-union action kind is EVASION_PROHIBITED", evasion_denied)
    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.act", {"session_id": sid2,
                                       "action": {"kind": "solve_captcha"}})
    check("C6 evasion kind rejected at schema validation",
          env is not None and env.get("error", {}).get("code") == "INVALID_ARGUMENTS",
          json.dumps(env)[:120] if env else "no envelope")

    # The user resolves the CAPTCHA in their own browser; the agent continues.
    resolved = operator.mark_challenge_resolved(sid2, by="user")
    check("C7 user resolution continues the session",
          resolved["continued_to"] == "mock://accounts/home"
          and resolved["observation"]["session_state"] == "active",
          json.dumps(resolved)[:120])
    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.observe", {"session_id": sid2})
    check("C8 post-challenge page is clean",
          model_view(env)["url"] == "mock://accounts/home"
          and model_view(env)["challenges"] == [], model_view(env)["url"])

    # -- D. commit barrier (mock purchase) -----------------------------------
    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.act", {"session_id": sid,
                                       "action": {"kind": "navigate",
                                                  "url": "mock://shop/product-1"}})
    obs = model_view(env)["observation"]
    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.act", {"session_id": sid,
                                       "action": {"kind": "click",
                                                  "element_id": el_id(obs, "Buy now")}})
    mv = model_view(env)
    proposal = mv.get("commit_proposal") or {}
    check("D1 consequential click proposes, never executes",
          mv.get("status") == "commit_proposed"
          and proposal.get("kind") == "browser.commit"
          and proposal["amount"]["minor_units"] == 12999
          and proposal.get("state_hash", "").startswith("sha256:"),
          json.dumps(mv)[:200])

    confirm_args = {"session_id": sid,
                    "action": {"kind": "confirm_commit",
                               "proposal_id": proposal["proposal_id"],
                               "state_hash": proposal["state_hash"]}}
    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.act", confirm_args)
    check("D2 confirm without approval is denied at R4",
          d == DENY and env is None, str(d))

    grant = approve_commit(approvals, decider4, ctx, proposal_bind_of(operator, sid),
                           confirm_args)
    ctx_g = ExecutionContext(run_id=ctx.run_id, tenant_id=ctx.tenant_id,
                             workspace_root=ctx.workspace_root,
                             event_log=ctx.event_log,
                             approval_grant_id=grant.id)
    d, env = call_tool(registry, policy, approvals, decider4, ctx_g,
                       "browser.act", confirm_args)
    mv = model_view(env)
    check("D3 fresh state-bound approval executes the commit",
          d == ALLOW and mv.get("status") == "commit_executed"
          and mv.get("amount_minor") == 12999, json.dumps(mv)[:160])
    check("D4 grant is single-use",
          approvals.grants[grant.id].used is True)

    d, env = call_tool(registry, policy, approvals, decider4, ctx_g,
                       "browser.act", confirm_args)
    check("D5 replaying a consumed grant is denied",
          d == DENY and env is None, str(d))

    # Tampered arguments: same grant scope, different args -> no valid grant.
    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.act", {"session_id": sid,
                                       "action": {"kind": "navigate",
                                                  "url": "mock://shop/review"}})
    obs = model_view(env)["observation"]
    # add an item so the review page has a priced cart
    call_tool(registry, policy, approvals, decider, ctx, "browser.act",
              {"session_id": sid, "action": {"kind": "navigate",
                                             "url": "mock://shop/product-2"}})
    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.observe", {"session_id": sid})
    obs2 = model_view(env)
    call_tool(registry, policy, approvals, decider, ctx, "browser.act",
              {"session_id": sid, "action": {"kind": "click",
                                             "element_id": el_id(obs2, "Add to cart")}})
    for url in ("mock://shop/cart",):
        d, env = call_tool(registry, policy, approvals, decider, ctx,
                           "browser.act", {"session_id": sid,
                                           "action": {"kind": "navigate", "url": url}})
    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.observe", {"session_id": sid})
    cart_obs = model_view(env)
    call_tool(registry, policy, approvals, decider, ctx, "browser.act",
              {"session_id": sid, "action": {"kind": "click",
                                             "element_id": el_id(cart_obs, "Proceed to checkout")}})
    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.observe", {"session_id": sid})
    review_obs = model_view(env)
    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.act", {"session_id": sid,
                                       "action": {"kind": "click",
                                                  "element_id": el_id(review_obs, "Place order")}})
    proposal2 = model_view(env).get("commit_proposal") or {}
    check("D6 review-page proposal priced from cart",
          model_view(env).get("status") == "commit_proposed"
          and proposal2["amount"]["minor_units"] == 3999,  # Gadget Lite
          json.dumps(proposal2.get("amount")))

    good_args = {"session_id": sid,
                 "action": {"kind": "confirm_commit",
                            "proposal_id": proposal2["proposal_id"],
                            "state_hash": proposal2["state_hash"]}}
    grant2 = approve_commit(approvals, decider4, ctx, proposal_bind_of(operator, sid),
                            good_args)
    tampered = {"session_id": sid,
                "action": {"kind": "confirm_commit",
                           "proposal_id": proposal2["proposal_id"] + "-evil",
                           "state_hash": proposal2["state_hash"]}}
    ctx_g2 = ExecutionContext(run_id=ctx.run_id, tenant_id=ctx.tenant_id,
                              workspace_root=ctx.workspace_root,
                              event_log=ctx.event_log,
                              approval_grant_id=grant2.id)
    d, env = call_tool(registry, policy, approvals, decider4, ctx_g2,
                       "browser.act", tampered)
    check("D7 tampered arguments invalidate the approval",
          d == DENY and env is None, str(d))

    # Page change invalidates the proposal even with a valid grant.
    grant3 = approve_commit(approvals, decider4, ctx, proposal_bind_of(operator, sid),
                            good_args)
    call_tool(registry, policy, approvals, decider, ctx, "browser.act",
              {"session_id": sid, "action": {"kind": "navigate",
                                             "url": "mock://news/"}})
    ctx_g3 = ExecutionContext(run_id=ctx.run_id, tenant_id=ctx.tenant_id,
                              workspace_root=ctx.workspace_root,
                              event_log=ctx.event_log,
                              approval_grant_id=grant3.id)
    d, env = call_tool(registry, policy, approvals, decider4, ctx_g3,
                       "browser.act", good_args)
    mv = model_view(env) if env else {}
    check("D8 navigation invalidates the pending proposal",
          d == ALLOW and mv.get("status") == "refused"
          and mv.get("code") == "NO_COMMIT_PROPOSAL", json.dumps(mv)[:160])

    # -- E. egress policy -----------------------------------------------------
    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.act", {"session_id": sid,
                                       "action": {"kind": "navigate",
                                                  "url": "https://evil.example/"}})
    mv = model_view(env)
    check("E1 non-mock navigation blocked by egress policy",
          mv.get("status") == "refused" and mv.get("code") == "EGRESS_DENIED",
          json.dumps(mv)[:120])

    # -- F. pacing -------------------------------------------------------------
    _, op_fast, _, _, _, _ = build(tempfile.mkdtemp(prefix="openmuse-pace-"),
                                   min_action_interval_s=3600.0)
    sid_f = op_fast.start_session("tenant_demo")
    op_fast.act(sid_f, {"kind": "navigate", "url": "mock://shop/"})
    try:
        op_fast.act(sid_f, {"kind": "navigate", "url": "mock://news/"})
        paced = False
    except BrowserError as exc:
        paced = exc.code == "PACING_VIOLATION"
    check("F1 back-to-back actions refused by pacing", paced)

    # -- G. checkpoint + recovery ----------------------------------------------
    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.act", {"session_id": sid,
                                       "action": {"kind": "navigate",
                                                  "url": "mock://shop/review"}})
    d, env = call_tool(registry, policy, approvals, decider, ctx,
                       "browser.checkpoint", {"session_id": sid,
                                              "label": "pre-commit"})
    ckpt = model_view(env)
    check("G1 checkpoint recorded",
          ckpt.get("url") == "mock://shop/review"
          and ckpt.get("checkpoint_id", "").startswith("ckpt_"),
          json.dumps(ckpt)[:120])
    call_tool(registry, policy, approvals, decider, ctx, "browser.act",
              {"session_id": sid, "action": {"kind": "navigate",
                                             "url": "mock://news/"}})
    rec = operator.recover(sid)
    check("G2 recovery resumes from checkpoint without replaying commits",
          rec["recovered_to"] == "mock://shop/review"
          and rec["commit_replayed"] is False
          and rec["observation"]["url"] == "mock://shop/review",
          json.dumps({k: rec[k] for k in ("recovered_to", "commit_replayed")}))
    check("G3 recovery drops any pending commit proposal",
          operator.pending_commit_proposal(sid) is None)

    # -- H. persistent profile --------------------------------------------------
    op2 = ManagedBrowserOperator(profile_root)
    attached = op2.attach_session(sid)
    check("H1 new operator instance re-attaches the persisted session",
          attached["url"] == "mock://shop/review"
          and attached["state"] == "active", json.dumps(attached))

    # -- I/J. policy mapping + observability ------------------------------------
    check("J1 risk mapping: observe=R0, act=R2",
          policy.risk_of("browser.observe") == "R0"
          and policy.risk_of("browser.act") == "R2")
    check("J2 confirm_commit escalates to R4",
          risk_for_action(policy, "browser.act",
                          {"action": {"kind": "confirm_commit"}}) == "R4")
    types = {e.type for e in log.events}
    check("J3 observability covers the browser lifecycle",
          {"browser.session_started", "browser.challenge_detected",
           "browser.commit_proposed", "browser.commit_executed",
           "browser.checkpoint"} <= types,
          str(sorted(types)))

    call_tool(registry, policy, approvals, decider, ctx,
              "browser.close_session", {"session_id": sid})
    call_tool(registry, policy, approvals, decider, ctx,
              "browser.close_session", {"session_id": sid2})

    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


def proposal_bind_of(operator: BrowserOperator, session_id: str) -> dict:
    proposal = operator.pending_commit_proposal(session_id)
    assert proposal is not None, "expected a pending commit proposal"
    return proposal.bind_fields()


if __name__ == "__main__":
    sys.exit(main())
