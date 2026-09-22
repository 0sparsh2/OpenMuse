"""Safety hardening and adversarial readiness (Phase 10).

Deterministic, dependency-free hardening layered on the Phase 1 policy core:

- risk_catalog: full R0–R5 catalog with coverage checks over every tool.
- taint: source-to-sink taint tracking for untrusted data.
- injection: prompt-injection classifier + deterministic destination checks.
- strong_auth: second-factor attestation for financial/security approvals.
- secret_scan: canary/secret scanners gating persistence and egress.
- redteam: red-team corpus + policy regression gate (blocks release).
- support: scoped, time-boxed support access + privacy-preserving diagnostics.
- runbooks.md: incident runbooks for policy-violation incidents.

Nothing here calls an LLM. Deterministic policy remains the final authority.
"""
from safety.risk_catalog import RiskCatalog, load_catalog
from safety.taint import TaintTracker, TaintSource, TaintFinding, SINK_KINDS
from safety.injection import (
    InjectionClassifier, InjectionFinding, classify_text,
    DestinationCheck, check_destination,
)
from safety.strong_auth import StrongAuthService, MockStrongAuthenticator
from safety.secret_scan import SecretScanner, seed_canary
from safety.redteam import RedTeamRunner, ReleaseBlocked, load_corpus
from safety.support import SupportAccessService, DiagnosticsBuilder, quarantine_run

__all__ = [
    "RiskCatalog", "load_catalog",
    "TaintTracker", "TaintSource", "TaintFinding", "SINK_KINDS",
    "InjectionClassifier", "InjectionFinding", "classify_text",
    "DestinationCheck", "check_destination",
    "StrongAuthService", "MockStrongAuthenticator",
    "SecretScanner", "seed_canary",
    "RedTeamRunner", "ReleaseBlocked", "load_corpus",
    "SupportAccessService", "DiagnosticsBuilder", "quarantine_run",
]
