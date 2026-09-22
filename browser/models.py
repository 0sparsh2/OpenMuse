"""Browser domain models — Phase 4.

Mirrors the blueprint's observation object, action contract, commit proposal,
and checkpoint schemas. The mock driver (mock_pages.py) and the managed
operator (operator.py) both speak these types; the tool namespace
(namespace.py) serializes them for the model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The action kinds the operator accepts. This is a closed union on purpose:
# anything outside it (e.g. "solve_captcha", "disable_bot_detection") is a
# deterministic EVASION_PROHIBITED refusal, never a policy question.
ACTION_KINDS = (
    "navigate", "click", "type", "select", "scroll", "upload",
    "download", "wait", "back", "confirm_commit",
)

# Kinds that are safe to run while a challenge is on screen.
CHALLENGE_SAFE_KINDS = ("wait",)

# Markers that identify a challenge page. Detection is pattern-based and
# conservative: on any hit the session pauses and hands to the user.
CHALLENGE_MARKERS = {
    "captcha": ("captcha", "recaptcha", "hcaptcha", "verify you are human",
                "i'm not a robot", "i am not a robot"),
    "cloudflare": ("cloudflare", "checking your browser", "attention required",
                   "cf_chl", "just a moment"),
    "login_wall": ("sign in to continue", "log in to continue",
                   "create an account to continue"),
    "two_factor": ("two-factor", "2fa", "verification code", "enter the code we sent"),
}


@dataclass
class PageElement:
    idx: int
    role: str            # link | button | textbox | checkbox | select | image
    name: str
    target: str = ""     # mock:// URL to navigate to on click ("" = no navigation)
    commit: dict = field(default_factory=dict)  # non-empty => consequential control
    form_field: str = "" # field id when role == "textbox"
    captcha: bool = False


@dataclass
class FormField:
    field_id: str
    label: str
    type: str            # text | email | password | search
    element_idx: int


@dataclass
class Challenge:
    kind: str            # captcha | cloudflare | login_wall | two_factor
    detected_via: str    # marker text that fired
    detected_at: str


@dataclass
class CommitProposal:
    proposal_id: str
    origin: str
    effect: str          # purchase | send | publish | delete | ...
    summary: str
    amount_minor: int = 0
    currency: str = "USD"
    destination: str = ""
    state_hash: str = ""
    navigation_id: int = 0
    url: str = ""
    created_at: str = ""

    def bind_fields(self) -> dict:
        """Fields an approval must bind to. Any page change invalidates them."""
        return {
            "origin": self.origin,
            "effect": self.effect,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "destination": self.destination,
            "state_hash": self.state_hash,
        }


@dataclass
class Checkpoint:
    checkpoint_id: str
    label: str
    url: str
    navigation_id: int
    dom_hash: str
    form_hashes: dict
    cart: list
    created_at: str


@dataclass
class BrowserSession:
    session_id: str
    tenant_id: str
    url: str = ""
    navigation_id: int = 0
    history: list[str] = field(default_factory=list)
    state: str = "active"          # active | challenged | closed
    challenge: Challenge | None = None
    form_state: dict = field(default_factory=dict)  # field_id -> {"value_hash":..., "value_set": True}
    cart: list = field(default_factory=list)
    pending_commit: CommitProposal | None = None
    checkpoints: list[Checkpoint] = field(default_factory=list)
    action_count: int = 0
    last_action_at: float = 0.0
    created_at: str = ""
    # never persisted in clear: quarantine entries reference scans, not content
    quarantine: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id, "tenant_id": self.tenant_id,
            "url": self.url, "navigation_id": self.navigation_id,
            "history": list(self.history), "state": self.state,
            "challenge": ({"kind": self.challenge.kind,
                           "detected_via": self.challenge.detected_via,
                           "detected_at": self.challenge.detected_at}
                          if self.challenge else None),
            "form_state": dict(self.form_state), "cart": list(self.cart),
            "pending_commit": (vars(self.pending_commit)
                               if self.pending_commit else None),
            "checkpoints": [vars(c) for c in self.checkpoints],
            "action_count": self.action_count,
            "last_action_at": self.last_action_at,
            "created_at": self.created_at,
            "quarantine": list(self.quarantine),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BrowserSession":
        s = cls(session_id=d["session_id"], tenant_id=d["tenant_id"])
        s.url = d.get("url", "")
        s.navigation_id = d.get("navigation_id", 0)
        s.history = d.get("history", [])
        s.state = d.get("state", "active")
        ch = d.get("challenge")
        s.challenge = Challenge(**ch) if ch else None
        s.form_state = d.get("form_state", {})
        s.cart = d.get("cart", [])
        pc = d.get("pending_commit")
        s.pending_commit = CommitProposal(**pc) if pc else None
        s.checkpoints = [Checkpoint(**c) for c in d.get("checkpoints", [])]
        s.action_count = d.get("action_count", 0)
        s.last_action_at = d.get("last_action_at", 0.0)
        s.created_at = d.get("created_at", "")
        s.quarantine = d.get("quarantine", [])
        return s
