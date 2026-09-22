"""
Deterministic policy engine — the final authorization authority.

Precedence (fixed, from the blueprint):
  1. Explicit DENY (R5, destructive guard, unknown mapping).
  2. Valid bound approval -> ALLOW.
  3. Narrow ALLOW for low-risk reads (R0, side_effect == none).
  4. Otherwise ASK (R1/R2, external writes) — or DENY when no rule matches.

The risk classifier (a model) may RAISE risk, never lower it. Phase 1 has no
classifier; risk comes from policies/tool-capabilities.yaml, which is the
deterministic baseline. Deterministic policy — not the LLM — is the final
authority.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

ALLOW = "ALLOW"
ASK = "ASK"
DENY = "DENY"

_RISK_ORDER = ["R0", "R1", "R2", "R3", "R4", "R5"]


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


@dataclass
class PolicyDecision:
    decision: str  # ALLOW | ASK | DENY
    reason_code: str
    safe_explanation: str
    approval_template: str = ""


class PolicyEngine:
    def __init__(self, capabilities_path: str):
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

        # 5. External communication and above are not wired in Phase 1.
        if inp.risk in ("R3", "R4"):
            return PolicyDecision(DENY, "NOT_IMPLEMENTED_PHASE1",
                                  f"{inp.tool_name} requires a Phase 3+ capability that is not enabled.")

        return PolicyDecision(DENY, "NO_MATCHING_RULE",
                              f"No policy rule allows {inp.tool_name}; failing closed.")
