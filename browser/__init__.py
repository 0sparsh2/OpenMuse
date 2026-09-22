"""Phase 4 — managed browser computer use.

ManagedBrowserOperator implements agent.seams.BrowserOperator against a
deterministic mock page driver (no network). browser.namespace wires the
browser.* tools into the tool runtime.
"""
from browser.models import ACTION_KINDS, CommitProposal
from browser.namespace import register, risk_for_action
from browser.operator import BrowserError, ManagedBrowserOperator

__all__ = [
    "ACTION_KINDS", "BrowserError", "CommitProposal",
    "ManagedBrowserOperator", "register", "risk_for_action",
]
