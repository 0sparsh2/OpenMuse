"""Red-team corpus runner + policy regression gate (Phase 10).

Each fixture in safety/fixtures/redteam_corpus.yaml pins an attack to its
expected policy outcome. run_all executes every fixture through the real
safety pipeline (taint -> injection classification -> destination check ->
secret scan -> policy engine -> approval binding) and compares the actual
outcome to the pinned expectation.

assert_release_gate raises ReleaseBlocked if anything differs — an attack
that bypasses policy, OR a control that gets over-blocked, blocks release.
The demo proves the gate is real by running the corpus against a
deliberately permissive policy and showing it fail loudly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

from policy.approvals import ApprovalService, AutoApproveDecider
from policy.engine import ALLOW, ASK, DENY, PolicyEngine, PolicyInput
from safety.injection import InjectionClassifier, check_destination
from safety.secret_scan import SecretScanner, seed_canary
from safety.taint import TaintTracker
from tools.executor import argument_hash

_CORPUS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "redteam_corpus.yaml")


class ReleaseBlocked(Exception):
    """Raised when the red-team gate fails: release is blocked."""


@dataclass
class FixtureResult:
    id: str
    title: str
    expected: str
    expected_reason: str
    actual: str
    actual_reason: str
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.actual == self.expected and self.actual_reason == self.expected_reason


def load_corpus(path: str = _CORPUS) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("fixtures", [])


class RedTeamRunner:
    def __init__(
        self,
        policy: PolicyEngine,
        *,
        taint: TaintTracker | None = None,
        classifier: InjectionClassifier | None = None,
        secrets: SecretScanner | None = None,
        approvals: ApprovalService | None = None,
        tool_version_of=None,
        capabilities_path: str = "",
    ):
        self.policy = policy
        self.taint = taint or TaintTracker()
        self.classifier = classifier or InjectionClassifier()
        self.secrets = secrets or SecretScanner()
        self.approvals = approvals or ApprovalService()
        self._tool_version_of = tool_version_of or (lambda name: "v1")

    # -- single fixture ------------------------------------------------------
    def run_fixture(self, fx: dict) -> FixtureResult:
        vector = fx["vector"]
        try:
            actual, reason, detail = self._execute(fx)
        except Exception as exc:  # a crashing fixture is a gate failure, not a pass
            actual, reason, detail = "ERROR", type(exc).__name__, str(exc)
        _ = vector
        return FixtureResult(
            id=fx["id"], title=fx["title"],
            expected=fx["expect"], expected_reason=fx["expect_reason"],
            actual=actual, actual_reason=reason, detail=detail,
        )

    def _policy_input(self, fx: dict, **overrides) -> PolicyInput:
        tool = fx["target_tool"]
        version = self._tool_version_of(tool)
        args = dict(fx.get("arguments", {}))
        risk = fx.get("tool_risk") or self.policy.risk_of(tool)
        ah = argument_hash(tool, version, args)
        return PolicyInput(
            tool_name=tool, tool_version=version, argument_hash=ah,
            risk=risk, capabilities=[],
            side_effect=self.policy.side_effect_of(tool),
            argument_summary={k: str(v)[:80] for k, v in args.items()},
            **overrides,
        )

    def _execute(self, fx: dict) -> tuple[str, str, str]:
        vector = fx["vector"]
        if vector in ("web", "email", "pdf", "child_output"):
            return self._execute_injection(fx)
        if vector == "arg_mutation":
            return self._execute_arg_mutation(fx)
        if vector == "redirect":
            return self._execute_redirect(fx)
        if vector == "seeded_credential":
            return self._execute_secret(fx)
        if vector == "taint":
            return self._execute_taint(fx)
        if vector == "suspect":
            return self._execute_suspect(fx)
        if vector == "control":
            return self._execute_control(fx)
        raise ValueError(f"unknown vector {vector!r}")

    def _execute_injection(self, fx: dict) -> tuple[str, str, str]:
        content = fx.get("content", "")
        finding = self.classifier.classify(content, source_kind=fx["vector"])
        taint_id = self.taint.ingest(fx["vector"], fx.get("source_origin", ""))
        dest = check_destination(fx["target_tool"], dict(fx.get("arguments", {})),
                                 intent=fx.get("intent", {}))
        taint_finding = self.taint.check_call(
            tool_name=fx["target_tool"], arguments=dict(fx.get("arguments", {})),
            taint_ids=(taint_id,), side_effect=self.policy.side_effect_of(fx["target_tool"]),
            risk=fx.get("tool_risk") or self.policy.risk_of(fx["target_tool"]),
        )
        inp = self._policy_input(
            fx,
            injection_severity=finding.severity,
            injection_rules=finding.matched_rules,
            destination_ok=dest.ok,
            destination_reason=dest.reason_code,
            destination_detail=dest.detail,
            tainted_sink=taint_finding.sink if taint_finding.tainted else "",
        )
        d = self.policy.evaluate(inp)
        return d.decision, d.reason_code, f"injection={finding.severity}"

    def _execute_arg_mutation(self, fx: dict) -> tuple[str, str, str]:
        tool = fx["target_tool"]
        version = self._tool_version_of(tool)
        approved_hash = argument_hash(tool, version, dict(fx["approved_args"]))
        req = self.approvals.create_request(
            run_id="run_redteam", tenant_id="tenant_redteam",
            tool_name=tool, tool_version=version, argument_hash=approved_hash,
            bind_fields={"repo": fx["approved_args"].get("repo", "")},
            risk=self.policy.risk_of(tool), template="external_write_v1",
        )
        # The user approves the ORIGINAL args...
        self.approvals.resolve(req.id, "approved", decided_by="user")
        # ...but the agent executes MUTATED args: grant lookup fails.
        executed_hash = argument_hash(tool, version, dict(fx["executed_args"]))
        grant = self.approvals.find_valid_grant(
            run_id="run_redteam", tool_name=tool, tool_version=version,
            argument_hash=executed_hash,
        )
        inp = self._policy_input(fx, has_valid_approval=grant is not None)
        # rebuild with the executed args' hash
        inp.argument_hash = executed_hash
        d = self.policy.evaluate(inp)
        return d.decision, d.reason_code, "grant_for_mutated_args_found=" + str(grant is not None)

    def _execute_redirect(self, fx: dict) -> tuple[str, str, str]:
        if fx.get("redirect_kind") == "server_redirect":
            r = fx["redirect"]
            dest = check_destination(
                "browser.redirect_reevaluate",
                {"from_origin": r["from_origin"], "to_origin": r["to_origin"]},
                intent=fx.get("intent", {}),
            )
            # A cross-origin redirect never silently continues: it is a DENY
            # that forces re-evaluation, asserted here at the check layer.
            return (DENY, dest.reason_code,
                    f"redirect {r['from_origin']} -> {r['to_origin']}")
        dest = check_destination(fx["target_tool"], dict(fx.get("arguments", {})),
                                 intent=fx.get("intent", {}))
        inp = self._policy_input(
            fx, destination_ok=dest.ok, destination_reason=dest.reason_code,
            destination_detail=dest.detail,
        )
        d = self.policy.evaluate(inp)
        return d.decision, d.reason_code, dest.detail

    def _execute_secret(self, fx: dict) -> tuple[str, str, str]:
        text = fx.get("secret_text", "")
        gate = fx.get("gate", "persist")
        res = self.secrets.scan_egress(text) if gate == "egress" else self.secrets.scan_persist(text)
        if res.blocked:
            return DENY, res.reason_code, f"labels={','.join(res.labels)}"
        return ALLOW, "NO_SECRET_FOUND", ""

    def _execute_taint(self, fx: dict) -> tuple[str, str, str]:
        taint_id = self.taint.ingest(fx["source_kind"], fx.get("source_origin", ""))
        risk = fx.get("tool_risk") or self.policy.risk_of(fx["target_tool"])
        finding = self.taint.check_call(
            tool_name=fx["target_tool"], arguments=dict(fx.get("arguments", {})),
            taint_ids=(taint_id,), side_effect=self.policy.side_effect_of(fx["target_tool"]),
            risk=risk,
        )
        inp = self._policy_input(
            fx, tainted_sink=finding.sink if finding.tainted else "",
        )
        # keep the fixture's explicit risk (e.g. R4 confirm_commit)
        inp.risk = risk
        d = self.policy.evaluate(inp)
        return d.decision, d.reason_code, f"sink={finding.sink}"

    def _execute_suspect(self, fx: dict) -> tuple[str, str, str]:
        finding = self.classifier.classify(fx.get("content", ""), source_kind=fx.get("source_kind", ""))
        inp = self._policy_input(
            fx, injection_severity=finding.severity,
            injection_rules=finding.matched_rules,
        )
        d = self.policy.evaluate(inp)
        return d.decision, d.reason_code, f"injection={finding.severity}"

    def _execute_control(self, fx: dict) -> tuple[str, str, str]:
        finding = self.classifier.classify(fx.get("content", ""), source_kind=fx.get("source_kind", ""))
        assert finding.severity == "none", f"control content flagged: {finding.matched_rules}"
        inp = self._policy_input(fx, injection_severity=finding.severity)
        d = self.policy.evaluate(inp)
        return d.decision, d.reason_code, "control"

    # -- gate ------------------------------------------------------------------
    def run_all(self, corpus: list[dict] | None = None) -> list[FixtureResult]:
        corpus = corpus if corpus is not None else load_corpus()
        return [self.run_fixture(fx) for fx in corpus]

    def assert_release_gate(self, results: list[FixtureResult]) -> None:
        failures = [r for r in results if not r.passed]
        if failures:
            lines = [f"  - {r.id}: expected {r.expected}/{r.expected_reason}, "
                     f"got {r.actual}/{r.actual_reason} ({r.detail})"
                     for r in failures]
            raise ReleaseBlocked(
                f"RELEASE BLOCKED: {len(failures)}/{len(results)} red-team "
                f"fixtures failed:\n" + "\n".join(lines)
            )
