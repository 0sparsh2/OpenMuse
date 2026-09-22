"""Approval UX: cards showing exact destination/effect with the bound
argument hash; device-auth gating for high-risk approvals.

Risk classes come from policies/risk-catalog.yaml: R3 = external
communication, R4 = financial/legal/security, R5 = destructive/prohibited.
R4/R5 are high-risk and require device authentication before the decision
can be submitted; after authentication the bound fields are re-displayed
and the decision binds to the exact argument hash the card showed.
"""
from __future__ import annotations

import abc
import hashlib
import json
from dataclasses import dataclass


HIGH_RISK_CLASSES = frozenset({"R4", "R5"})


def requires_device_auth(risk: str) -> bool:
    return risk in HIGH_RISK_CLASSES


def canonical_hash(arguments: dict) -> str:
    """The canonical argument hash the approval binds to."""
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DeviceAuth(abc.ABC):
    """Device authentication challenge (WebAuthn/biometric on the real client)."""

    @abc.abstractmethod
    def authenticate(self, *, reason: str) -> str:
        """Run the device challenge. Returns an opaque auth token."""


class TestDeviceAuth(DeviceAuth):
    """Demo/test double: a device challenge that always succeeds."""

    def __init__(self, token: str = "test-device-auth-token"):
        self.token = token
        self.challenges: list[str] = []

    def authenticate(self, *, reason: str) -> str:
        self.challenges.append(reason)
        return self.token


def _render_destination_effect(tool_name: str, bind: dict) -> tuple[str, str]:
    """Derive the exact destination and effect from secret-free bind fields."""
    b = {str(k): v for k, v in bind.items()}
    renderers = {
        "files.write": lambda: (str(b.get("path", "?")),
                                f"write {b.get('bytes', b.get('size', '?'))} "
                                f"bytes to {b.get('path', '?')}"),
        "files.read": lambda: (str(b.get("path", "?")),
                               f"read {b.get('path', '?')}"),
        "browser.act": lambda: (str(b.get("url", b.get("origin", "?"))),
                                f"{b.get('action', 'act')} on "
                                f"{b.get('url', b.get('origin', '?'))}"),
        "email.send": lambda: (str(b.get("to", "?")),
                               f"send email to {b.get('to', '?')}"),
        "message.send": lambda: (str(b.get("to", b.get("channel", "?"))),
                                 f"send message to {b.get('to', b.get('channel', '?'))}"),
    }
    fn = renderers.get(tool_name)
    if fn is not None:
        return fn()
    dest = b.get("destination") or b.get("path") or b.get("url") \
        or b.get("to") or b.get("target") or "?"
    effect = b.get("effect") or f"{tool_name} with {len(b)} bound argument(s)"
    return str(dest), str(effect)


@dataclass
class ApprovalCard:
    """What the UI renders for one pending approval."""
    approval_id: str
    run_id: str
    tool_name: str
    tool_version: str
    risk: str
    destination: str        # exact destination (e.g. recipient, path, URL)
    effect: str             # human-readable effect summary
    argument_hash: str      # the hash the decision binds to
    bind_fields: dict       # secret-free bound fields, re-displayed
    expires_in_seconds: int = 900
    device_authed: bool = False

    @staticmethod
    def from_server(payload: dict) -> "ApprovalCard":
        """Build a card from the GET /v1/approvals/{id} response shape."""
        bind = dict(payload.get("bind_fields",
                                payload.get("presentation", {})))
        destination, effect = _render_destination_effect(
            payload.get("tool_name", payload.get("tool", "")), bind)
        return ApprovalCard(
            approval_id=payload["approval_id"],
            run_id=payload.get("run_id", ""),
            tool_name=payload.get("tool_name", payload.get("tool", "")),
            tool_version=payload.get("tool_version", ""),
            risk=payload.get("risk", ""),
            destination=destination,
            effect=effect,
            argument_hash=payload["argument_hash"],
            bind_fields=bind,
            expires_in_seconds=int(payload.get("expires_in_seconds", 900)))

    def render(self) -> dict:
        """The exact card content the UI shows (and re-shows after auth)."""
        return {
            "approval_id": self.approval_id,
            "tool": f"{self.tool_name}@{self.tool_version}",
            "risk": self.risk,
            "destination": self.destination,
            "effect": self.effect,
            "argument_hash": self.argument_hash,
            "bind_fields": self.bind_fields,
            "device_authed": self.device_authed,
        }

    def ensure_authorized(self, device_auth: DeviceAuth | None) -> "ApprovalCard":
        """Gate high-risk decisions behind device authentication.

        On success the bound fields are re-displayed (returned card has
        device_authed=True); without a challenge the decision must not
        be submitted.
        """
        if requires_device_auth(self.risk):
            if device_auth is None:
                raise PermissionError(
                    f"approval {self.approval_id} is {self.risk}: "
                    "device authentication is required")
            token = device_auth.authenticate(
                reason=f"Approve {self.tool_name} ({self.risk}) -> {self.destination}")
            if not token:
                raise PermissionError("device authentication failed")
            self.device_authed = True
        return self
