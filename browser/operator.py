"""Managed browser operator — Phase 4.

Implements agent.seams.BrowserOperator against a deterministic mock driver
(mock_pages) so the whole loop runs offline. The same operator contract
drives a real Chromium worker in deployment; the mock keeps Phase 4
testable without network access.

Blueprint rules enforced here (deterministically, not by the model):

- Observation-grounded actions: element ids are `el_<nav>_<n>` and die with
  the navigation that created them. Stale ids are rejected (STALE_ELEMENT).
- No evasion: the action union is closed. Clicking/typing into challenge
  controls, or any non-union kind, is EVASION_PROHIBITED. The agent never
  solves CAPTCHAs; the session pauses and hands to the user.
- Commit barrier: consequential controls ("Buy now", "Place order") do NOT
  execute on click. They return a browser.commit proposal bound to
  (origin, effect, amount, state_hash). Execution needs kind=confirm_commit
  plus a valid approval whose bind fields match the CURRENT page state.
  Any navigation invalidates the proposal.
- Conservative pacing: a minimum interval between actions and a per-session
  action cap. Violations are refused, not queued.
- Credential hygiene: password fields accept only opaque textRefs and only
  under a valid approval; the operator stores a hash, never the value, and
  observations/logs never contain it.
- Checkpoints & recovery: checkpoints record url/nav/dom+form hashes/cart.
  Recovery restores the last checkpoint, verifies state, and NEVER replays
  a commit — a pending proposal is dropped and must be re-proposed.
- Persistent profiles: session state (cookies jar simulated) persists under
  profile_root, so a new operator instance can attach and continue.
- Downloads enter quarantine and are content-scanned (deterministic stub)
  before becoming workspace artifacts.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from agent.seams import BrowserObservation, BrowserOperator

from browser import mock_pages
from browser.models import (
    ACTION_KINDS,
    CHALLENGE_SAFE_KINDS,
    BrowserSession,
    Challenge,
    Checkpoint,
    CommitProposal,
    FormField,
    PageElement,
)

HOME_URL = "mock://news/"
MAX_ACTIONS_PER_SESSION = 120  # blueprint budget default


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class BrowserError(Exception):
    """Deterministic operator refusal. code is stable for tests/policy."""

    def __init__(self, code: str, message: str, *, extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.extra = extra or {}


class ManagedBrowserOperator(BrowserOperator):
    """Single-tenant mock browser worker with a persistent profile."""

    def __init__(self, profile_root: str, *,
                 min_action_interval_s: float = 0.0,
                 max_actions_per_session: int = MAX_ACTIONS_PER_SESSION,
                 time_fn=time.time):
        self.profile_root = profile_root
        self.min_action_interval_s = min_action_interval_s
        self.max_actions_per_session = max_actions_per_session
        self._time = time_fn
        self._sessions: dict[str, BrowserSession] = {}
        os.makedirs(os.path.join(profile_root, "sessions"), exist_ok=True)
        # profile.json holds the (simulated) cookie jar; 0600. Envelope
        # encryption of this blob is a deployment concern (Phase 8).
        self._profile_path = os.path.join(profile_root, "profile.json")
        if not os.path.exists(self._profile_path):
            self._write_profile({"cookies": {}, "created_at": _utcnow()})
        os.chmod(self._profile_path, 0o600)

    # ------------------------------------------------------------------
    # profile + session persistence
    # ------------------------------------------------------------------
    def _write_profile(self, data: dict) -> None:
        with open(self._profile_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def _read_profile(self) -> dict:
        with open(self._profile_path, encoding="utf-8") as fh:
            return json.load(fh)

    def _session_path(self, session_id: str) -> str:
        return os.path.join(self.profile_root, "sessions", session_id + ".json")

    def _persist(self, session: BrowserSession) -> None:
        with open(self._session_path(session.session_id), "w", encoding="utf-8") as fh:
            json.dump(session.to_dict(), fh, ensure_ascii=False)

    def _load(self, session_id: str) -> BrowserSession:
        if session_id in self._sessions:
            return self._sessions[session_id]
        path = self._session_path(session_id)
        if not os.path.exists(path):
            raise BrowserError("UNKNOWN_SESSION",
                               f"no such browser session: {session_id}")
        with open(path, encoding="utf-8") as fh:
            session = BrowserSession.from_dict(json.load(fh))
        self._sessions[session_id] = session
        return session

    # ------------------------------------------------------------------
    # lifecycle (seam)
    # ------------------------------------------------------------------
    def start_session(self, tenant_id: str) -> str:
        session_id = "br_" + uuid.uuid4().hex[:12]
        session = BrowserSession(session_id=session_id, tenant_id=tenant_id,
                                 created_at=_utcnow())
        self._sessions[session_id] = session
        self._navigate(session, HOME_URL)
        self._persist(session)
        return session_id

    def attach_session(self, session_id: str) -> dict:
        session = self._load(session_id)
        if session.state == "closed":
            raise BrowserError("SESSION_CLOSED", "session is closed")
        return {"session_id": session_id, "url": session.url,
                "state": session.state,
                "navigation_id": session.navigation_id}

    def close_session(self, session_id: str) -> dict:
        session = self._load(session_id)
        session.state = "closed"
        session.pending_commit = None
        self._persist(session)
        return {"session_id": session_id, "closed": True}

    def mark_challenge_resolved(self, session_id: str, *, by: str = "user") -> dict:
        """The USER resolved the challenge out-of-band (own browser).

        The agent never "solves" anything: this records the user's action and
        continues the session past the challenge page.
        """
        session = self._load(session_id)
        if session.state != "challenged" or not session.challenge:
            raise BrowserError("NO_CHALLENGE",
                               "session is not waiting on a challenge")
        page = mock_pages.get_page(session.url) or {}
        dest = page.get("challenge_resolves_to")
        if not dest:
            raise BrowserError("CHALLENGE_UNRESOLVABLE",
                               "this challenge has no continuation page")
        kind = session.challenge.kind
        session.challenge = None
        session.state = "active"
        self._navigate(session, dest)
        self._persist(session)
        return {"session_id": session_id, "challenge": kind,
                "resolved_by": by, "continued_to": dest,
                "observation": self._observation_view(session)}

    # ------------------------------------------------------------------
    # seam: observe / act
    # ------------------------------------------------------------------
    def observe(self, session_id: str) -> BrowserObservation:
        session = self._load(session_id)
        if session.state == "closed":
            raise BrowserError("SESSION_CLOSED", "session is closed")
        return self._observation(session)

    def act(self, session_id: str, action: dict) -> dict:
        session = self._load(session_id)
        if session.state == "closed":
            raise BrowserError("SESSION_CLOSED", "session is closed")
        if not isinstance(action, dict) or action.get("kind") not in ACTION_KINDS:
            raise BrowserError("EVASION_PROHIBITED",
                               f"action kind {action.get('kind')!r} is outside the "
                               "allowed browser action union")
        kind = action["kind"]

        # Pacing: conservative rate discipline, refused not queued.
        now = self._time()
        if now - session.last_action_at < self.min_action_interval_s:
            raise BrowserError("PACING_VIOLATION",
                               "acting too fast; wait before the next action")
        if session.action_count >= self.max_actions_per_session:
            raise BrowserError("ACTION_BUDGET_EXHAUSTED",
                               "per-session browser action budget exhausted")

        # Challenge gate: while challenged, only observation-safe kinds run.
        if session.state == "challenged" and kind not in CHALLENGE_SAFE_KINDS:
            return self._challenge_envelope(session)

        try:
            if kind == "navigate":
                result = self._do_navigate(session, action)
            elif kind == "click":
                result = self._do_click(session, action)
            elif kind == "type":
                result = self._do_type(session, action)
            elif kind == "select":
                result = self._do_select(session, action)
            elif kind == "scroll":
                result = self._do_scroll(session, action)
            elif kind == "wait":
                result = {"waited_ms": int(action.get("timeout_ms", 1000))}
            elif kind == "back":
                result = self._do_back(session)
            elif kind == "download":
                result = self._do_download(session, action)
            elif kind == "upload":
                result = self._do_upload(session, action)
            elif kind == "confirm_commit":
                result = self._do_confirm_commit(session, action)
            else:  # pragma: no cover — union is closed above
                raise BrowserError("INVALID_ACTION", f"unknown kind {kind!r}")
        except BrowserError:
            raise
        session.action_count += 1
        session.last_action_at = now
        # A consequential page state change drops nothing silently: any
        # pending commit whose state hash no longer matches is invalidated.
        if session.pending_commit and not self._commit_still_fresh(session):
            session.pending_commit = None
        self._persist(session)
        envelope = {"status": "ok", "action": {"kind": kind},
                    "observation": self._observation_view(session)}
        envelope.update(result)
        return envelope

    # ------------------------------------------------------------------
    # checkpoints & recovery
    # ------------------------------------------------------------------
    def checkpoint(self, session_id: str, label: str) -> dict:
        session = self._load(session_id)
        cp = Checkpoint(
            checkpoint_id="ckpt_" + uuid.uuid4().hex[:10], label=label,
            url=session.url, navigation_id=session.navigation_id,
            dom_hash=self._dom_hash(session),
            form_hashes={fid: v["value_hash"]
                         for fid, v in session.form_state.items()},
            cart=list(session.cart), created_at=_utcnow())
        session.checkpoints.append(cp)
        self._persist(session)
        return {"checkpoint_id": cp.checkpoint_id, "label": label,
                "url": cp.url}

    def recover(self, session_id: str) -> dict:
        """Resume from the last checkpoint and verify state.

        Never replays a commit: a pending proposal is dropped and must be
        re-proposed from the verified page.
        """
        session = self._load(session_id)
        if not session.checkpoints:
            raise BrowserError("NO_CHECKPOINT", "no checkpoint to recover from")
        cp = session.checkpoints[-1]
        dropped_commit = session.pending_commit is not None
        session.pending_commit = None
        self._navigate(session, cp.url)
        # verify: the restored page must match the checkpoint's DOM hash
        # modulo dynamic fields (cart summary is re-rendered deterministically)
        verified = self._dom_hash(session) == cp.dom_hash
        session.cart = list(cp.cart)
        self._persist(session)
        return {"recovered_to": cp.url, "checkpoint_id": cp.checkpoint_id,
                "state_verified": verified,
                "commit_replayed": False,
                "dropped_pending_commit": dropped_commit,
                "observation": self._observation_view(session)}

    def cart_of(self, session_id: str) -> list:
        return list(self._load(session_id).cart)

    def pending_commit_proposal(self, session_id: str):
        """The current pending commit proposal, if any (None otherwise)."""
        session = self._load(session_id)
        return session.pending_commit

    # ------------------------------------------------------------------
    # navigation & page model
    # ------------------------------------------------------------------
    def _do_navigate(self, session: BrowserSession, action: dict) -> dict:
        url = action.get("url", "")
        # Deterministic egress policy for the mock driver: only mock://
        # origins are reachable. Real deployments enforce this at the proxy.
        if not url.startswith("mock://"):
            raise BrowserError("EGRESS_DENIED",
                               f"navigation to {url!r} blocked by egress policy")
        if mock_pages.get_page(url) is None:
            raise BrowserError("NAVIGATION_FAILED",
                               f"no such mock page: {url}")
        self._navigate(session, url)
        return {"navigated_to": url}

    def _navigate(self, session: BrowserSession, url: str) -> None:
        if session.url:
            session.history.append(session.url)
        session.url = url
        session.navigation_id += 1
        self._detect_challenge(session)

    def _do_back(self, session: BrowserSession) -> dict:
        if not session.history:
            raise BrowserError("NO_HISTORY", "no page to go back to")
        url = session.history.pop()
        session.url = url
        session.navigation_id += 1
        self._detect_challenge(session)
        return {"navigated_to": url}

    def _detect_challenge(self, session: BrowserSession) -> None:
        page = mock_pages.get_page(session.url) or {}
        hits = mock_pages.detect_challenges(page)
        if hits:
            hit = hits[0]
            session.state = "challenged"
            session.challenge = Challenge(kind=hit["kind"],
                                          detected_via=hit["detected_via"],
                                          detected_at=_utcnow())
        elif session.state == "challenged" and not session.challenge:
            session.state = "active"

    def _challenge_envelope(self, session: BrowserSession) -> dict:
        ch = session.challenge
        return {
            "status": "challenge_paused",
            "handoff_to_user": {
                "challenge": ch.kind if ch else "unknown",
                "detected_via": ch.detected_via if ch else "",
                "url": session.url,
                "instruction": (
                    "A challenge is blocking this page. Per your CAPTCHA "
                    "setting, the agent will not attempt to bypass it. "
                    "Please resolve it in your own browser, then tell the "
                    "assistant you are done so it can continue."),
            },
            "observation": self._observation_view(session),
        }

    # ------------------------------------------------------------------
    # grounded actions
    # ------------------------------------------------------------------
    def _resolve_element(self, session: BrowserSession,
                         element_id: str) -> tuple[PageElement, dict]:
        """element_id -> (PageElement, raw dict). Enforces freshness."""
        try:
            _, nav_s, idx_s = element_id.split("_")
            nav_id, idx = int(nav_s), int(idx_s)
        except (ValueError, AttributeError):
            raise BrowserError("INVALID_ELEMENT",
                               f"malformed element id: {element_id!r}")
        if nav_id != session.navigation_id:
            raise BrowserError("STALE_ELEMENT",
                               f"element {element_id!r} belongs to navigation "
                               f"{nav_id}; current is {session.navigation_id}. "
                               "Re-observe before acting.")
        page = mock_pages.get_page(session.url) or {}
        raw_list = page.get("elements", [])
        if not (0 <= idx < len(raw_list)):
            raise BrowserError("INVALID_ELEMENT",
                               f"no element {element_id!r} on this page")
        raw = raw_list[idx]
        return (PageElement(idx=idx, role=raw.get("role", ""),
                            name=raw.get("name", ""),
                            target=raw.get("target", ""),
                            commit=raw.get("commit", {}),
                            form_field=raw.get("form_field", ""),
                            captcha=bool(raw.get("captcha"))), raw)

    def _do_click(self, session: BrowserSession, action: dict) -> dict:
        element, raw = self._resolve_element(session, action.get("element_id", ""))
        if element.captcha:
            # Clicking a CAPTCHA checkbox programmatically IS bypass.
            raise BrowserError("EVASION_PROHIBITED",
                               "challenge controls may only be resolved by the user")
        if element.commit:
            return self._propose_commit(session, element)
        if raw.get("cart_add"):
            session.cart.append(raw["cart_add"])
            return {"cart_added": raw["cart_add"], "cart": list(session.cart)}
        if element.target:
            self._do_navigate(session, {"url": element.target})
            return {"navigated_to": element.target, "clicked": element.name}
        return {"clicked": element.name}

    def _do_type(self, session: BrowserSession, action: dict) -> dict:
        element, _ = self._resolve_element(session, action.get("element_id", ""))
        if element.captcha:
            raise BrowserError("EVASION_PROHIBITED",
                               "challenge inputs may only be completed by the user")
        if element.role != "textbox" or not element.form_field:
            raise BrowserError("INVALID_ACTION",
                               f"element {action.get('element_id')!r} is not a text field")
        field = self._field_def(session, element.form_field)
        text = action.get("text", "")
        text_ref = action.get("text_ref", "")
        if field.type == "password":
            # Credential fill: opaque reference only, under a fresh approval
            # (checked by the tool layer via ctx.approval_grant_id). The value
            # itself never enters the session, observation, or logs.
            if not text_ref or text:
                raise BrowserError(
                    "CREDENTIAL_FILL_NEEDS_APPROVAL",
                    "password fields accept only an opaque text_ref under a "
                    "fresh approval; the model never handles the secret value")
            if not action.get("approved_credential_fill"):
                raise BrowserError(
                    "CREDENTIAL_FILL_NEEDS_APPROVAL",
                    "credential fill requires a fresh approval")
            session.form_state[field.field_id] = {
                "value_hash": _sha("credential:" + text_ref), "value_set": True}
            submit = bool(action.get("submit"))
            if submit and element.target:
                self._do_navigate(session, {"url": element.target})
            return {"typed_into": field.label, "value_set": True,
                    "credential": True}
        if not text:
            raise BrowserError("INVALID_ACTION", "type requires 'text'")
        session.form_state[field.field_id] = {
            "value_hash": _sha("field:" + text), "value_set": True}
        return {"typed_into": field.label, "value_set": True}

    def _field_def(self, session: BrowserSession, field_id: str) -> FormField:
        page = mock_pages.get_page(session.url) or {}
        for form in page.get("forms", []):
            for f in form.get("fields", []):
                if f["field_id"] == field_id:
                    # element_idx is informational here; kept for the view
                    return FormField(field_id=field_id, label=f["label"],
                                     type=f["type"], element_idx=-1)
        raise BrowserError("INVALID_ELEMENT",
                           f"no field {field_id!r} on this page")

    def _do_select(self, session: BrowserSession, action: dict) -> dict:
        element, _ = self._resolve_element(session, action.get("element_id", ""))
        if element.role != "select":
            raise BrowserError("INVALID_ACTION", "element is not a select")
        return {"selected": action.get("option", ""), "element": element.name}

    def _do_scroll(self, session: BrowserSession, action: dict) -> dict:
        return {"scrolled": action.get("direction", "down"),
                "amount": action.get("amount", "page")}

    def _do_download(self, session: BrowserSession, action: dict) -> dict:
        element, raw = self._resolve_element(session, action.get("element_id", ""))
        # Quarantine first; deterministic content scan; then artifact ref.
        entry = {"quarantine_id": "qz_" + uuid.uuid4().hex[:10],
                 "element": element.name,
                 "scan": "clean", "scanned_at": _utcnow()}
        session.quarantine.append(entry)
        return {"quarantined": entry["quarantine_id"], "scan": "clean",
                "artifact_ref": f"obj://browser/downloads/{entry['quarantine_id']}"}

    def _do_upload(self, session: BrowserSession, action: dict) -> dict:
        element, _ = self._resolve_element(session, action.get("element_id", ""))
        artifact_ref = action.get("artifact_ref", "")
        if not artifact_ref:
            raise BrowserError("INVALID_ACTION", "upload requires artifact_ref")
        if not action.get("approved_upload"):
            raise BrowserError("UPLOAD_NEEDS_APPROVAL",
                               "uploads require an explicit artifact reference "
                               "and destination approval")
        return {"uploaded": artifact_ref, "to_element": element.name}

    # ------------------------------------------------------------------
    # commit barrier
    # ------------------------------------------------------------------
    def _state_hash(self, session: BrowserSession) -> str:
        payload = json.dumps({
            "url": session.url, "nav": session.navigation_id,
            "cart": session.cart,
            "forms": {k: v["value_hash"]
                      for k, v in sorted(session.form_state.items())},
        }, sort_keys=True)
        return _sha(payload)

    def _dom_hash(self, session: BrowserSession) -> str:
        page = mock_pages.get_page(session.url) or {}
        return _sha(page.get("a11y", ""))

    def _propose_commit(self, session: BrowserSession,
                        element: PageElement) -> dict:
        spec = dict(element.commit)
        amount = spec.get("amount_minor", 0)
        if spec.get("cart_priced"):
            amount = self._cart_total_minor(session)
            spec["summary"] = (f"Place order for cart "
                               f"({len(session.cart)} items)")
        proposal = CommitProposal(
            proposal_id="prop_" + uuid.uuid4().hex[:10],
            origin="mock", effect=spec.get("effect", "unknown"),
            summary=spec.get("summary", element.name),
            amount_minor=amount, currency=spec.get("currency", "USD"),
            destination=spec.get("destination", ""),
            state_hash=self._state_hash(session),
            navigation_id=session.navigation_id, url=session.url,
            created_at=_utcnow())
        session.pending_commit = proposal
        return {
            "status": "commit_proposed",
            "commit_proposal": {
                "kind": "browser.commit",
                "proposal_id": proposal.proposal_id,
                "origin": proposal.origin, "effect": proposal.effect,
                "summary": proposal.summary,
                "amount": {"currency": proposal.currency,
                           "minor_units": proposal.amount_minor},
                "destination": proposal.destination,
                "state_hash": proposal.state_hash,
                "note": ("This action did NOT execute. Confirm with a fresh, "
                         "state-bound approval to proceed."),
            },
        }

    def _cart_total_minor(self, session: BrowserSession) -> int:
        prices = {"Widget Pro": 12999, "Gadget Lite": 3999}
        return sum(prices.get(item, 0) for item in session.cart)

    def _commit_still_fresh(self, session: BrowserSession) -> bool:
        p = session.pending_commit
        return bool(p) and p.state_hash == self._state_hash(session) \
            and p.navigation_id == session.navigation_id

    def _do_confirm_commit(self, session: BrowserSession,
                           action: dict) -> dict:
        proposal = session.pending_commit
        if proposal is None:
            raise BrowserError("NO_COMMIT_PROPOSAL",
                               "no pending commit proposal for this session")
        if action.get("proposal_id") != proposal.proposal_id:
            raise BrowserError("COMMIT_PROPOSAL_MISMATCH",
                               "proposal id does not match the pending proposal")
        if not self._commit_still_fresh(session):
            session.pending_commit = None
            raise BrowserError("COMMIT_STALE",
                               "page state changed since the proposal; "
                               "re-propose from the current page")
        # The tool layer guarantees a valid bound approval before this runs
        # (R4 + has_valid_approval, or the namespace's explicit grant check).
        if not action.get("approved_commit"):
            raise BrowserError("COMMIT_NEEDS_APPROVAL",
                               "commit execution requires a fresh state-bound approval")
        session.pending_commit = None
        session.cart = []
        return {"status": "commit_executed", "effect": proposal.effect,
                "summary": proposal.summary,
                "amount_minor": proposal.amount_minor}

    # ------------------------------------------------------------------
    # observation views
    # ------------------------------------------------------------------
    def _observation(self, session: BrowserSession) -> BrowserObservation:
        page = mock_pages.get_page(session.url) or {}
        nav = session.navigation_id
        elements = []
        for idx, raw in enumerate(page.get("elements", [])):
            elements.append({
                "element_id": f"el_{nav}_{idx}",
                "role": raw.get("role", ""), "name": raw.get("name", ""),
            })
        forms = []
        for form in page.get("forms", []):
            fields = []
            for f in form.get("fields", []):
                st = session.form_state.get(f["field_id"], {})
                fields.append({"field_id": f["field_id"], "label": f["label"],
                               "type": f["type"],
                               # hashes/flags only — values never surface
                               "value_set": bool(st.get("value_set"))})
            forms.append({"form_id": form["form_id"], "fields": fields})
        a11y = page.get("a11y", "")
        if "{cart_summary}" in a11y:
            summary = ", ".join(session.cart) if session.cart else "(empty)"
            a11y = a11y.replace("{cart_summary}", summary)
        if "{cart_total}" in a11y:
            total = self._cart_total_minor(session) / 100
            a11y = a11y.replace("{cart_total}", f"${total:.2f}")
        return BrowserObservation(
            url=session.url, title=page.get("title", ""),
            accessibility_tree=a11y, navigation_id=nav, origin="mock",
            interactive_elements=elements, forms=forms,
            challenges=[session.challenge.kind] if session.challenge else [],
            session_state=session.state, captured_at=_utcnow())

    def _observation_view(self, session: BrowserSession) -> dict:
        obs = self._observation(session)
        return {
            "session_id": session.session_id, "url": obs.url,
            "title": obs.title, "origin": obs.origin,
            "navigation_id": obs.navigation_id,
            "accessibility_tree": obs.accessibility_tree,
            "interactive_elements": obs.interactive_elements,
            "forms": obs.forms, "challenges": obs.challenges,
            "session_state": obs.session_state,
            "cart": list(session.cart),
            "captured_at": obs.captured_at,
        }
