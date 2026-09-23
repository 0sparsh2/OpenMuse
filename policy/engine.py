"""
Deterministic policy engine — the final authorization authority.

Precedence (fixed, from the blueprint):
  1. Explicit DENY (R5, destructive guard, unknown mapping).
  1b. Safety hardening (Phase 10): block-severity injection -> DENY;
      destination mismatch -> DENY; R4 without strong-auth attestation -> ASK;
      tainted data at a sensitive sink without clearance -> ASK (R4+: DENY);
      suspect-severity injection without human review -> ASK.
  2. Valid bound approval -> ALLOW.
  3. Narrow ALLOW for low-risk reads (R0, side_effect == none).
  4. Otherwise ASK (R1/R2, external writes) — or DENY when no rule matches.

The risk classifier (deterministic rules in safety/) may RAISE risk, never
lower it. Deterministic policy — not the LLM — is the final authority.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

ALLOW = "ALLOW"
ASK = "ASK"
DENY = "DENY"

_RISK_ORDER = ["R0", "R1", "R2", "R3", "R4", "R5"]

# Injection severity levels produced by safety.injection.
INJ_NONE = "none"
INJ_SUSPECT = "suspect"
INJ_BLOCK = "block"


@dataclass
class PolicyInput:
    tool_name: str
    tool_version: str
    argument_hash: str
    risk: str
    capabilities: list[str]
    side_effect: str
    # human-readable summary for approval cards (secret values already redacted upstream)
    argument_summary: dict = field(default_factory=dict)
    has_valid_approval: bool = False
    # --- Phase 10 safety fields (all defaulted: existing callers unaffected) ---
    # Prompt-injection screening of the untrusted content behind this call.
    injection_severity: str = INJ_NONE
    injection_rules: tuple = ()
    injection_reviewed: bool = False  # human reviewed a suspect finding
    # Deterministic destination check (origin / recipient / path vs intent).
    destination_ok: bool = True
    destination_reason: str = ""      # e.g. CROSS_ORIGIN_REDIRECT
    destination_detail: str = ""
    # Source-to-sink taint: tainted data reaching a sensitive sink.
    tainted_sink: str = ""            # e.g. "external_send"; "" = no tainted sink
    taint_cleared: bool = False       # user explicitly re-authorized the taint
    # Strong authentication for financial/security (R4) actions.
    requires_strong_auth: bool = False
    strong_auth_attested: bool = False


@dataclass
class PolicyDecision:
    decision: str  # ALLOW | ASK | DENY
    reason_code: str
    safe_explanation: str
    approval_template: str = ""


class PolicyEngine:
    def __init__(self, capabilities_path: str, *, ask_for_external_writes: bool = False):
        # False (blueprint default): R3 runs only with an approval granted out
        # of band. True (interactive app): R3 is ASKed inline — one bound,
        # single-use approval per exact call; nothing auto-approves it.
        self.ask_for_external_writes = ask_for_external_writes
        with open(capabilities_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
        self._tools: dict = (cfg or {}).get("tools", {})

    # -- risk helpers ------------------------------------------------------
    def risk_of(self, tool_name: str) -> str:
        entry = self._tools.get(tool_name)
        if entry is None:
            return "R5"  # unmapped tools fail closed
        return entry.get("risk", "R5")

    def raise_risk(self, current: str, raised: str) -> str:
        """Classifier input: may raise risk, never lower it."""
        if _RISK_ORDER.index(raised) > _RISK_ORDER.index(current):
            return raised
        return current

    def side_effect_of(self, tool_name: str) -> str:
        return self._tools.get(tool_name, {}).get("side_effect", "destructive")

    # -- evaluation ----------------------------------------------------------
    def evaluate(self, inp: PolicyInput) -> PolicyDecision:
        # 1. Explicit deny: prohibited class.
        if inp.risk == "R5":
            return PolicyDecision(DENY, "PROHIBITED_RISK_CLASS",
                                  f"{inp.tool_name} is in a prohibited risk class and cannot run.")

        # 1b. Safety hardening: these hold regardless of approvals, because an
        # approval binds arguments — it cannot sanitize an attack payload,
        # re-authorize a changed destination, or substitute for a second factor.
        if inp.injection_severity == INJ_BLOCK:
            rules = ", ".join(inp.injection_rules) or "unspecified"
            return PolicyDecision(
                DENY, "INJECTION_BLOCKED",
                f"Blocked: untrusted content behind {inp.tool_name} matches "
                f"prompt-injection rules ({rules}). No approval can authorize this; "
                "the content must be removed or the task reframed.")

        if not inp.destination_ok:
            return PolicyDecision(
                DENY, inp.destination_reason or "DESTINATION_MISMATCH",
                f"Blocked: {inp.destination_detail or 'destination is outside the authorized intent'}. "
                "Re-authorize the new destination to continue.")

        if inp.requires_strong_auth and not inp.strong_auth_attested:
            return PolicyDecision(
                ASK, "STRONG_AUTH_REQUIRED",
                f"{inp.tool_name} is a financial/security action and needs a "
                "second-factor attestation before it can run.",
                approval_template="strong_auth_v1")

        if inp.tainted_sink and not inp.taint_cleared:
            if inp.risk in ("R4", "R5"):
                return PolicyDecision(
                    DENY, "TAINTED_SINK_BLOCKED",
                    f"Blocked: tainted data would reach the {inp.tainted_sink} sink "
                    f"of {inp.tool_name}. The user must explicitly clear the taint first.")
            return PolicyDecision(
                ASK, "TAINT_CLEARANCE_REQUIRED",
                f"Tainted data would reach the {inp.tainted_sink} sink of "
                f"{inp.tool_name}. The user must explicitly re-authorize it.",
                approval_template="taint_clearance_v1")

        if inp.injection_severity == INJ_SUSPECT and not inp.injection_reviewed:
            return PolicyDecision(
                ASK, "INJECTION_REVIEW_REQUIRED",
                f"Untrusted content behind {inp.tool_name} looks suspicious "
                f"({', '.join(inp.injection_rules) or 'heuristic match'}). "
                "A human must review it before this call runs.",
                approval_template="injection_review_v1")

        # 2. Valid bound approval authorizes the exact proposed call.
        if inp.has_valid_approval:
            return PolicyDecision(ALLOW, "BOUND_APPROVAL",
                                  f"{inp.tool_name} authorized by a bound approval.")

        # 3. Narrow allow: low-risk reads with no side effect.
        if inp.risk == "R0" and inp.side_effect == "none":
            return PolicyDecision(ALLOW, "LOW_RISK_READ",
                                  f"{inp.tool_name} is a low-risk read.")

        # 4. Reversible local writes and private reads need an explicit grant.
        if inp.risk in ("R1", "R2"):
            template = "local_write_v1" if inp.side_effect == "local_write" else "private_read_v1"
            return PolicyDecision(
                ASK, "APPROVAL_REQUIRED", approval_template=template,
                safe_explanation=f"{inp.tool_name} needs your approval before it runs.")

        # 5. External communication (R3: send an email, create an event) asks the
        #    user every time via a single-use approval bound to these exact
        #    arguments. (No decider auto-approves R3; AutonomousDecider holds it.)
        if inp.risk == "R3" and self.ask_for_external_writes:
            return PolicyDecision(
                ASK, "APPROVAL_REQUIRED", approval_template="external_write_v1",
                safe_explanation=f"{inp.tool_name} acts outside OpenMuse and needs your approval.")

        # 6. Financial / legal (R4) and above need a valid bound approval that
        #    was already granted (strong-auth path above); never asked inline.
        if inp.risk in ("R3", "R4"):
            return PolicyDecision(DENY, "APPROVAL_REQUIRED_HIGH_RISK",
                                  f"{inp.tool_name} is high-risk and needs a "
                                  "valid bound approval to run.")

        return PolicyDecision(DENY, "NO_MATCHING_RULE",
                              f"No policy rule allows {inp.tool_name}; failing closed.")
