"""Full R0–R5 risk catalog accessor (Phase 10).

Loads policies/risk-catalog.yaml and verifies coverage: every tool declared
in policies/tool-capabilities.yaml must map to a known risk class, and every
class must document description, examples, and treatment. Gaps fail loudly —
an undocumented class or an unmapped tool blocks release via the red-team
gate (safety/redteam.py).
"""
from __future__ import annotations

import os

import yaml

RISK_ORDER = ["R0", "R1", "R2", "R3", "R4", "R5"]

_DEFAULT_CATALOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "policies", "risk-catalog.yaml",
)
_DEFAULT_CAPABILITIES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "policies", "tool-capabilities.yaml",
)


class CatalogError(ValueError):
    """Raised when the catalog is incomplete or inconsistent."""


class RiskCatalog:
    def __init__(self, catalog_path: str = _DEFAULT_CATALOG):
        with open(catalog_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        self._classes: dict = cfg.get("risk_classes", {})
        self._escalation: dict = cfg.get("escalation", {})
        self._validate()

    def _validate(self) -> None:
        missing = [c for c in RISK_ORDER if c not in self._classes]
        if missing:
            raise CatalogError(f"risk catalog missing classes: {missing}")
        for cls in RISK_ORDER:
            entry = self._classes[cls] or {}
            for field in ("description", "examples", "treatment"):
                if not entry.get(field):
                    raise CatalogError(f"risk class {cls} missing documented {field!r}")

    # -- accessors ---------------------------------------------------------
    def describe(self, risk: str) -> str:
        return self._classes[risk]["description"]

    def examples(self, risk: str) -> list[str]:
        return list(self._classes[risk]["examples"])

    def treatment(self, risk: str) -> str:
        return self._classes[risk]["treatment"]

    def approval_requirement(self, risk: str) -> str:
        return self._classes[risk].get("approval", "")

    def escalation_rules(self) -> dict:
        return dict(self._escalation)

    def classes(self) -> list[str]:
        return list(RISK_ORDER)

    def raise_only(self, current: str, proposed: str) -> str:
        """The classifier may raise risk, never lower it."""
        if RISK_ORDER.index(proposed) > RISK_ORDER.index(current):
            return proposed
        return current

    # -- coverage ----------------------------------------------------------
    def check_tool_coverage(self, capabilities_path: str = _DEFAULT_CAPABILITIES) -> dict:
        """Every tool in tool-capabilities.yaml must map to a known class.

        Returns {"tools": N, "unmapped": [...], "unknown_class": [...]};
        raises CatalogError when coverage is incomplete.
        """
        with open(capabilities_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        tools = cfg.get("tools", {})
        unmapped, unknown_class = [], []
        for name, entry in tools.items():
            risk = (entry or {}).get("risk")
            if not risk:
                unmapped.append(name)
            elif risk not in self._classes:
                unknown_class.append((name, risk))
        if unmapped or unknown_class:
            raise CatalogError(
                f"tool coverage incomplete: unmapped={unmapped} "
                f"unknown_class={unknown_class}"
            )
        return {"tools": len(tools), "unmapped": [], "unknown_class": []}


def load_catalog(catalog_path: str = _DEFAULT_CATALOG) -> RiskCatalog:
    return RiskCatalog(catalog_path)
