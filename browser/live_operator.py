"""Live browser operator — real Chromium via Playwright.

Implements the same agent.seams.BrowserOperator contract as the mock
ManagedBrowserOperator, so browser.namespace drives it unchanged:

- Observation-grounded actions: every observe() tags visible interactive
  elements with `el_<gen>_<n>` ids. Ids from an older observation are
  rejected (STALE_ELEMENT) — re-observe first.
- Commit barrier: clicking a consequential control (buy / pay / place order /
  book / checkout) does not execute; it returns a commit proposal. Only
  kind=confirm_commit with an approved grant clicks it.
- No evasion: CAPTCHA / bot-check pages pause the session and hand off to
  the user, who resolves them from the live viewer (user_input()).
- Credential hygiene: password fields are never typed by the agent.

Live view: after every action (and on an idle timer) the worker captures a
JPEG frame. Clients poll session_info() / frame() to watch in real time,
and can take over with user_input() (click / type / key / scroll).

Playwright's sync API is bound to the thread that started it, so every
browser call is marshalled onto one dedicated worker thread.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
import uuid
from concurrent.futures import Future
from datetime import datetime, timezone
from urllib.parse import urlparse

from agent.seams import BrowserObservation, BrowserOperator
from browser.models import ACTION_KINDS, CommitProposal
from browser.operator import BrowserError

VIEWPORT = {"width": 1280, "height": 800}
MAX_ELEMENTS = 120
MAX_TEXT_CHARS = 6000
IDLE_CAPTURE_S = 0.8
IDLE_SESSION_TIMEOUT_S = 15 * 60
LOG_LIMIT = 200
USER_CONTROL_WAIT_S = 60  # stays under browser.act's tool timeout

COMMIT_RE = re.compile(
    r"\b(buy( now)?|place (your )?order|pay( now)?|purchase|complete (booking|purchase|order)|"
    r"confirm (and pay|booking|purchase|order)|book now|submit order|checkout)\b", re.I)
CHALLENGE_RE = re.compile(
    r"(verify (that )?you are (a )?human|unusual traffic|are you a robot|press (&|and) hold|"
    r"complete the security check|captcha|checking your browser|made by a human|complete the following challenge|confirm you.re (a )?human)", re.I)

OBSERVE_JS = r"""
(gen) => {
  const SEL = 'a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link],' +
    '[role=tab],[role=option],[role=menuitem],[role=checkbox],[role=radio],[role=combobox],' +
    '[role=switch],[role=gridcell],[contenteditable=true],[onclick],summary';
  document.querySelectorAll('[data-om-id]').forEach(e => e.removeAttribute('data-om-id'));
  const out = []; let n = 0; const H = innerHeight;
  for (const e of document.querySelectorAll(SEL)) {
    const r = e.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || r.bottom < -H || r.top > 2.5 * H) continue;
    const st = getComputedStyle(e);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    const id = 'el_' + gen + '_' + (n++);
    e.setAttribute('data-om-id', id);
    const tag = e.tagName.toLowerCase();
    const role = e.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'select' ? 'combobox' : tag);
    let name = (e.getAttribute('aria-label') || e.innerText || e.getAttribute('placeholder') ||
                e.getAttribute('title') || e.getAttribute('alt') || e.getAttribute('name') || '')
               .trim().replace(/\s+/g, ' ').slice(0, 80);
    let line = id + ' ' + role + (e.type && tag === 'input' ? '[' + e.type + ']' : '') + ' "' + name + '"';
    if ((tag === 'input' || tag === 'textarea') && e.type !== 'password' && e.value)
      line += ' value="' + String(e.value).slice(0, 60) + '"';
    if (tag === 'select' && e.selectedOptions && e.selectedOptions[0])
      line += ' selected="' + e.selectedOptions[0].text.slice(0, 40) + '"';
    if (r.top >= H || r.bottom <= 0) line += ' (offscreen)';
    out.push(line);
    if (n >= %d) break;
  }
  const text = document.body ? document.body.innerText.replace(/[ \t]+/g, ' ')
      .replace(/\n\s*\n+/g, '\n').slice(0, %d) : '';
  const pw = !!document.querySelector('input[type=password]');
  const cap = !!document.querySelector('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],' +
      'iframe[src*="challenges.cloudflare"],#px-captcha,.g-recaptcha,.h-captcha');
  return {elements: out, text, title: document.title, scrollY: Math.round(scrollY),
          scrollH: document.documentElement.scrollHeight, innerH: H, password: pw, captcha: cap};
}
""" % (MAX_ELEMENTS, MAX_TEXT_CHARS)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class _Session:
    def __init__(self, session_id: str, tenant_id: str, user_id: str = ""):
        self.session_id = session_id
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.context = None
        self.page = None
        self.gen = 0
        self.state = "active"          # active | challenged | closed
        self.challenge = ""
        self.frame: bytes = b""
        self.frame_hash = ""
        self.frame_seq = 0
        self.frame_at = 0.0
        self.cursor: dict | None = None  # last pointer position {x, y, actor}
        self.log: list[dict] = []
        self.pending_commit: CommitProposal | None = None
        self.pending_commit_el = ""
        self.checkpoints: list[dict] = []
        self.last_used = time.time()
        self.url = "about:blank"
        self.title = ""
        self.controller = "agent"       # agent | user  (user = "Take control")
        self.user_touched = False       # user acted since the agent last looked
        self.failures: dict[str, int] = {}
        self.downloads: list[dict] = []


class LiveBrowserOperator(BrowserOperator):
    # Each user gets their own cookie/login profile; sessions are owned.
    per_user_profiles = True

    def __init__(self, profile_root: str, *, headless: bool = True,
                 start_url: str = "about:blank"):
        self.profile_root = profile_root
        # Set by the host: resolve a vault ref to a secret for (user, ref, page_url)
        # (browser/logins.py), and receive finished downloads (user, name, bytes)
        # -> artifact id (the Library). Both optional.
        self.credential_resolver = None
        self.credential_describer = None   # (user, ref) -> {"site","username","field"} (no secret)
        self.download_sink = None
        self.headless = headless
        self.start_url = start_url
        os.makedirs(profile_root, exist_ok=True)
        self._storage_path = os.path.join(profile_root, "storage_state.json")  # legacy / no user
        self._sessions: dict[str, _Session] = {}
        self._q: queue.Queue = queue.Queue()
        self._pw = None
        self._browser = None
        self._stopped = False
        self._thread = threading.Thread(target=self._worker, daemon=True,
                                        name="live-browser")
        self._thread.start()

    # ------------------------------------------------------------------
    # worker thread
    # ------------------------------------------------------------------
    def _worker(self) -> None:
        while not self._stopped:
            try:
                fn, fut = self._q.get(timeout=0.25)
            except queue.Empty:
                self._idle()
                continue
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn())
                except BaseException as exc:  # noqa: BLE001 - surfaced to caller
                    fut.set_exception(exc)
            self._idle()

    def _call(self, fn, timeout: float = 90.0):
        fut: Future = Future()
        self._q.put((fn, fut))
        return fut.result(timeout=timeout)

    def _idle(self) -> None:
        now = time.time()
        for s in list(self._sessions.values()):
            if s.state == "closed" or s.page is None:
                continue
            if now - s.last_used > IDLE_SESSION_TIMEOUT_S:
                self._close(s)
                continue
            if now - s.frame_at >= IDLE_CAPTURE_S:
                self._capture(s)

    def _ensure_browser(self) -> None:
        if self._browser is not None:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)

    # ------------------------------------------------------------------
    # helpers (worker thread only)
    # ------------------------------------------------------------------
    def _get(self, session_id: str) -> _Session:
        s = self._sessions.get(session_id)
        if s is None:
            raise BrowserError("UNKNOWN_SESSION", f"no such browser session: {session_id}")
        if s.state == "closed":
            raise BrowserError("SESSION_CLOSED", "session is closed")
        s.last_used = time.time()
        return s

    def _capture(self, s: _Session) -> None:
        try:
            data = s.page.screenshot(type="jpeg", quality=60, timeout=5000)
        except Exception:
            s.frame_at = time.time()
            return
        s.frame_at = time.time()
        h = hashlib.sha1(data).hexdigest()
        if h != s.frame_hash:
            s.frame, s.frame_hash = data, h
            s.frame_seq += 1
        try:
            s.url, s.title = s.page.url, s.page.title()
        except Exception:
            pass

    def _log(self, s: _Session, label: str, *, actor: str = "agent", kind: str = "") -> None:
        s.log.append({"label": label, "kind": kind, "actor": actor,
                      "url": s.page.url if s.page else "", "at": time.time()})
        del s.log[:-LOG_LIMIT]

    def _settle(self, s: _Session, ms: int = 2500) -> None:
        try:
            s.page.wait_for_load_state("domcontentloaded", timeout=ms)
        except Exception:
            pass
        try:
            s.page.wait_for_load_state("networkidle", timeout=ms)
        except Exception:
            pass

    def _observe(self, s: _Session) -> dict:
        s.gen += 1
        try:
            raw = s.page.evaluate(OBSERVE_JS, s.gen)
        except Exception:
            # page mid-navigation: settle and retry once
            self._settle(s, 3000)
            raw = s.page.evaluate(OBSERVE_JS, s.gen)
        blob = (raw.get("title", "") + "\n" + raw.get("text", "")[:3000])
        challenged = raw.get("captcha") or bool(CHALLENGE_RE.search(blob))
        if challenged and s.state != "challenged":
            s.state, s.challenge = "challenged", "captcha"
            self._log(s, "Paused — human verification needed", kind="challenge")
        elif not challenged and s.state == "challenged":
            s.state, s.challenge = "active", ""
        self._capture(s)
        s.url, s.title = s.page.url, raw.get("title", "")
        return raw

    def _obs_from_raw(self, s: _Session, raw: dict) -> BrowserObservation:
        scroll = f"[scroll {raw.get('scrollY', 0)}/{raw.get('scrollH', 0)}px, viewport {raw.get('innerH', 0)}px]"
        return BrowserObservation(
            url=s.page.url, title=raw.get("title", ""),
            accessibility_tree=scroll + "\n" + raw.get("text", ""),
            screenshot_ref=f"frame://{s.session_id}/{s.frame_seq}",
            navigation_id=s.gen,
            origin=_origin(s.page.url),
            interactive_elements=raw.get("elements", []),
            forms=[],
            challenges=[s.challenge] if s.state == "challenged" else [],
            session_state=s.state,
            captured_at=_utcnow(),
        )

    def _element(self, s: _Session, element_id: str):
        m = re.fullmatch(r"el_(\d+)_(\d+)", element_id or "")
        if not m:
            raise BrowserError("BAD_ELEMENT", f"malformed element id: {element_id!r}")
        if int(m.group(1)) != s.gen:
            raise BrowserError("STALE_ELEMENT",
                               "element id is from an older observation; call browser.observe "
                               "and use a fresh id")
        loc = s.page.locator(f'[data-om-id="{element_id}"]').first
        if loc.count() == 0:
            raise BrowserError("STALE_ELEMENT", "element is no longer on the page; re-observe")
        return loc

    def _point_at(self, s: _Session, loc, actor: str = "agent") -> None:
        try:
            loc.scroll_into_view_if_needed(timeout=3000)
            box = loc.bounding_box(timeout=3000)
            if box:
                s.cursor = {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2,
                            "actor": actor, "at": time.time()}
        except Exception:
            pass

    def _on_download(self, s: _Session, download) -> None:
        """Save a finished download into the user's Library (after its checks)."""
        name = download.suggested_filename or "download"
        try:
            path = download.path()  # waits for the download to finish
            with open(path, "rb") as fh:
                data = fh.read()
            if self.download_sink is None:
                self._log(s, f"Downloaded {name} (no Library configured)", kind="download")
                return
            aid = self.download_sink(s.user_id, name, data)
            s.downloads.append({"name": name, "artifact_id": aid})
            self._log(s, f"Saved {name} to your Library", kind="download")
        except Exception as exc:
            self._log(s, f"Download blocked: {str(exc)[:120]}", kind="download")

    def _adopt_page(self, s: _Session, page) -> None:
        """A link opened a new tab/popup: follow it (agents otherwise get stuck)."""
        s.page = page
        page.on("download", lambda d, s=s: self._on_download(s, d))
        page.on("close", lambda _p, s=s: self._on_page_closed(s))
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        self._log(s, f"Switched to new tab · {_host(page.url)}", kind="tab")

    def _on_page_closed(self, s: _Session) -> None:
        open_pages = [p for p in s.context.pages if not p.is_closed()] if s.context else []
        if open_pages:
            s.page = open_pages[-1]

    def _close(self, s: _Session) -> None:
        try:
            path = self._profile_path(s.user_id)
            s.context.storage_state(path=path)
            os.chmod(path, 0o600)
        except Exception:
            pass
        try:
            s.context.close()
        except Exception:
            pass
        s.state, s.page, s.pending_commit = "closed", None, None

    # ------------------------------------------------------------------
    # seam: lifecycle
    # ------------------------------------------------------------------
    def _profile_path(self, user_id: str) -> str:
        if not user_id:
            return self._storage_path
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", user_id)[:80]
        d = os.path.join(self.profile_root, "users", safe)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "storage_state.json")

    def start_session(self, tenant_id: str, user_id: str = "") -> str:
        def run():
            self._ensure_browser()
            sid = "br_" + uuid.uuid4().hex[:12]
            s = _Session(sid, tenant_id, user_id)
            kw = {"viewport": VIEWPORT, "locale": "en-US", "accept_downloads": True}
            profile = self._profile_path(user_id)
            if os.path.exists(profile):
                kw["storage_state"] = profile  # this user's persistent profile
            s.context = self._browser.new_context(**kw)
            s.page = s.context.new_page()
            s.context.on("page", lambda pg, s=s: self._adopt_page(s, pg))
            s.page.on("download", lambda d, s=s: self._on_download(s, d))
            if self.start_url and self.start_url != "about:blank":
                s.page.goto(self.start_url, wait_until="domcontentloaded", timeout=30000)
            self._sessions[sid] = s
            self._log(s, "Opened browser", kind="start")
            self._capture(s)
            return sid
        return self._call(run)

    def close_session(self, session_id: str) -> dict:
        def run():
            s = self._get(session_id)
            self._log(s, "Closed browser", kind="close")
            self._capture(s)
            self._close(s)
            return {"session_id": session_id, "closed": True}
        return self._call(run)

    def attach_session(self, session_id: str) -> dict:
        s = self._sessions.get(session_id)
        if s is None or s.state == "closed":
            raise BrowserError("SESSION_CLOSED", "session is closed or unknown")
        return {"session_id": session_id, "url": s.url, "state": s.state,
                "navigation_id": s.gen}

    def mark_challenge_resolved(self, session_id: str, *, by: str = "user") -> dict:
        def run():
            s = self._get(session_id)
            s.state, s.challenge = "active", ""
            self._log(s, "Verification completed by you", actor=by, kind="challenge")
            return {"session_id": session_id, "state": s.state}
        return self._call(run)

    def checkpoint(self, session_id: str, label: str) -> dict:
        def run():
            s = self._get(session_id)
            cp = {"checkpoint_id": "cp_" + uuid.uuid4().hex[:10], "label": label,
                  "url": s.page.url}
            s.checkpoints.append(cp)
            return cp
        return self._call(run)

    def recover(self, session_id: str) -> dict:
        def run():
            s = self._get(session_id)
            if not s.checkpoints:
                raise BrowserError("NO_CHECKPOINT", "no checkpoint to recover from")
            cp = s.checkpoints[-1]
            s.pending_commit = None  # commits are never replayed
            s.page.goto(cp["url"], wait_until="domcontentloaded", timeout=30000)
            self._settle(s)
            self._log(s, f"Recovered to checkpoint “{cp['label']}”", kind="recover")
            return {"recovered_to": cp["checkpoint_id"], "url": s.page.url}
        return self._call(run)

    # ------------------------------------------------------------------
    # seam: observe / act
    # ------------------------------------------------------------------
    def observe(self, session_id: str) -> BrowserObservation:
        def run():
            s = self._get(session_id)
            return self._obs_from_raw(s, self._observe(s))
        return self._call(run)

    def credential_info(self, session_id: str, ref: str) -> dict | None:
        """Non-secret description of a saved-login ref, for the approval card."""
        s = self._sessions.get(session_id)
        if s is None or self.credential_describer is None:
            return None
        return self.credential_describer(s.user_id, ref)

    def pending_commit_proposal(self, session_id: str):
        s = self._sessions.get(session_id)
        return s.pending_commit if s else None

    def cart_of(self, session_id: str) -> list:
        return []

    def act(self, session_id: str, action: dict) -> dict:
        s = self._sessions.get(session_id)
        waited = False
        if s is not None and s.controller == "user":
            # The user took control from the live view: hold the agent's
            # action until they hand back (never fight the user for the page).
            deadline = time.time() + USER_CONTROL_WAIT_S
            while s.controller == "user" and time.time() < deadline and s.state != "closed":
                waited = True
                time.sleep(0.5)
            if s.controller == "user":
                return {"status": "refused", "code": "USER_IN_CONTROL",
                        "message": "The user is controlling the browser right now. Do not act; "
                                   "tell the user you'll continue when they hand control back, "
                                   "or use kind=wait and try again."}
        intervened = bool(s and (waited or s.user_touched))
        try:
            out = self._call(lambda: self._act(session_id, action), timeout=85.0)
        except BrowserError as exc:
            if s is not None:
                key = f"{action.get('kind')}:{exc.code}"
                s.failures[key] = s.failures.get(key, 0) + 1
                if s.failures[key] >= 2:
                    exc.args = (str(exc) + " — this has now failed "
                                f"{s.failures[key]} times; change approach (re-observe, scroll, "
                                "use a different element, or navigate to a more specific URL).",)
            raise
        if s is not None:
            s.failures.clear()
            s.user_touched = False
        if intervened:
            out["user_intervened"] = ("The user took control of the browser since your last "
                                      "step and may have changed the page. Trust the current "
                                      "observation over your earlier plan.")
        return out

    def _act(self, session_id: str, action: dict) -> dict:
        s = self._get(session_id)
        kind = action.get("kind", "")
        if kind not in ACTION_KINDS:
            raise BrowserError("EVASION_PROHIBITED", f"action kind {kind!r} is not allowed")
        if s.state == "challenged" and kind not in ("wait", "back", "navigate"):
            return {"status": "challenge_paused",
                    "handoff_to_user": {"challenge": s.challenge,
                                        "message": "A human-verification check is showing. "
                                                   "Ask the user to open the live browser and "
                                                   "complete it, then wait and observe again."}}
        page = s.page
        out: dict = {"status": "ok", "action": {k: v for k, v in action.items()
                                                if k not in ("text_ref",)}}

        if kind == "navigate":
            url = (action.get("url") or "").strip()
            if not url:
                raise BrowserError("BAD_ARGUMENT", "navigate needs a url")
            if not re.match(r"^[a-z][a-z0-9+.-]*:", url):
                url = "https://" + url
            if not url.startswith(("http://", "https://")):
                raise BrowserError("EGRESS_DENIED", "only http(s) URLs are allowed")
            self._log(s, f"Opening {_host(url)}", kind="navigate")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                raise BrowserError("NAVIGATION_FAILED", str(exc).splitlines()[0][:200])
            s.pending_commit = None
            self._settle(s)
            out["navigated_to"] = page.url

        elif kind == "click":
            loc = self._element(s, action.get("element_id", ""))
            label = _label_of(loc)
            if COMMIT_RE.search(label):
                s.pending_commit = CommitProposal(
                    proposal_id="cp_" + uuid.uuid4().hex[:10],
                    origin=_origin(page.url), effect="purchase",
                    summary=f"“{label}” on {_host(page.url)}",
                    amount_minor=_find_amount(page), currency="USD",
                    destination=_host(page.url),
                    state_hash=_sha(page.url + "|" + label),
                    navigation_id=s.gen, url=page.url, created_at=_utcnow(),
                )
                s.pending_commit_el = action.get("element_id", "")
                self._point_at(s, loc)
                self._log(s, f"Waiting for your approval to “{label}”", kind="commit")
                self._capture(s)
                prop = dict(s.pending_commit.__dict__)
                return {"status": "commit_proposed", "commit_proposal": prop,
                        "message": "This control has a real-world effect. It was NOT clicked. "
                                   "Tell the user what it will do and ask them to approve."}
            self._point_at(s, loc)
            self._log(s, f"Clicking “{label or 'element'}”", kind="click")
            self._capture(s)
            pages_before = len(s.context.pages)
            try:
                loc.click(timeout=8000)
            except Exception as exc:
                raise BrowserError("CLICK_FAILED", str(exc).splitlines()[0][:200])
            # a click's navigation/popup starts asynchronously: give it a beat
            # before waiting for load, or we'd observe the previous page
            page.wait_for_timeout(700)
            if len(s.context.pages) > pages_before and s.page is page:
                self._adopt_page(s, s.context.pages[-1])
            self._settle(s, 3000)
            out["clicked"] = label

        elif kind == "type" and action.get("text_ref"):
            # Saved-login fill: approved per call, origin-checked, value never
            # logged / observed / returned (password inputs render masked).
            if not action.get("approved_credential_fill"):
                raise BrowserError("CREDENTIAL_FILL_NEEDS_APPROVAL", "signing in needs the user's approval")
            if self.credential_resolver is None:
                raise BrowserError("CREDENTIAL_FILL_UNSUPPORTED",
                                   "no saved logins here; ask the user to sign in from the live view")
            loc = self._element(s, action.get("element_id", ""))
            try:
                secret = self.credential_resolver(s.user_id, action["text_ref"], page.url)
            except PermissionError as exc:
                raise BrowserError("CREDENTIAL_ORIGIN_MISMATCH", str(exc))
            except ValueError as exc:
                raise BrowserError("CREDENTIAL_UNAVAILABLE", str(exc))
            is_password = action["text_ref"].endswith("#password")
            label = _label_of(loc) if not is_password else "password"
            self._point_at(s, loc)
            self._log(s, "Entering your saved " + ("password" if is_password else "username"), kind="type")
            try:
                loc.click(timeout=5000)
                loc.fill(secret, timeout=5000)
                secret = ""
                if action.get("submit"):
                    page.keyboard.press("Enter")
                    self._settle(s, 3000)
            except Exception as exc:
                raise BrowserError("TYPE_FAILED", str(exc).splitlines()[0][:200])
            out["typed_into"] = label
            out["credential"] = True

        elif kind == "type":
            loc = self._element(s, action.get("element_id", ""))
            if (loc.get_attribute("type") or "").lower() == "password":
                raise BrowserError("CREDENTIAL_FIELD",
                                   "never type passwords as text; use a saved login ref from "
                                   "browser.logins (text_ref), or ask the user to sign in from the live view")
            text = action.get("text", "")
            label = _label_of(loc)
            self._point_at(s, loc)
            self._log(s, f"Typing “{text[:40]}” into {label or 'field'}", kind="type")
            try:
                loc.click(timeout=5000)
                try:
                    loc.fill(text, timeout=5000)
                except Exception:
                    page.keyboard.press("ControlOrMeta+a")
                    page.keyboard.type(text, delay=20)
                if action.get("submit"):
                    page.keyboard.press("Enter")
                    self._settle(s, 3000)
                else:
                    page.wait_for_timeout(600)  # let autocomplete dropdowns render
            except BrowserError:
                raise
            except Exception as exc:
                raise BrowserError("TYPE_FAILED", str(exc).splitlines()[0][:200])
            out["typed_into"] = label

        elif kind == "select":
            loc = self._element(s, action.get("element_id", ""))
            opt = action.get("option", "")
            self._point_at(s, loc)
            self._log(s, f"Selecting “{opt}”", kind="select")
            try:
                try:
                    loc.select_option(label=opt, timeout=5000)
                except Exception:
                    loc.select_option(value=opt, timeout=5000)
            except Exception as exc:
                raise BrowserError("SELECT_FAILED", str(exc).splitlines()[0][:200])
            self._settle(s, 1500)
            out["selected"] = opt

        elif kind == "scroll":
            direction = action.get("direction", "down")
            frac = 0.5 if action.get("amount") == "half" else 0.9
            dy = int(VIEWPORT["height"] * frac) * (-1 if direction == "up" else 1)
            self._log(s, f"Scrolling {direction}", kind="scroll")
            page.mouse.wheel(0, dy)
            page.wait_for_timeout(500)
            out["scrolled"] = direction

        elif kind == "wait":
            ms = min(int(action.get("timeout_ms") or 1500), 30000)
            self._log(s, "Waiting for the page", kind="wait")
            page.wait_for_timeout(ms)
            out["waited_ms"] = ms

        elif kind == "back":
            self._log(s, "Going back", kind="back")
            page.go_back(wait_until="domcontentloaded", timeout=20000)
            s.pending_commit = None
            self._settle(s)

        elif kind == "confirm_commit":
            if not action.get("approved_commit") or s.pending_commit is None:
                raise BrowserError("COMMIT_NEEDS_APPROVAL", "no approved commit to execute")
            loc = self._element(s, s.pending_commit_el)
            self._point_at(s, loc)
            self._log(s, f"Approved — {s.pending_commit.summary}", kind="commit")
            loc.click(timeout=8000)
            self._settle(s, 4000)
            s.pending_commit = None
            out["status"] = "commit_executed"
            out["effect"] = "clicked approved control"

        else:  # upload / download
            raise BrowserError("NOT_SUPPORTED", f"{kind} is not available in the live browser")

        raw = self._observe(s)
        obs = self._obs_from_raw(s, raw)
        out["observation"] = {
            "session_id": session_id, "url": obs.url, "title": obs.title,
            "origin": obs.origin, "navigation_id": obs.navigation_id,
            "accessibility_tree": obs.accessibility_tree,
            "interactive_elements": obs.interactive_elements,
            "forms": [], "challenges": obs.challenges,
            "session_state": obs.session_state, "cart": [],
            "captured_at": obs.captured_at,
        }
        if s.state == "challenged":
            out["status"] = "challenge_paused"
            out["handoff_to_user"] = {"challenge": s.challenge,
                                      "message": "Human verification required. Ask the user to "
                                                 "open the live browser and complete it."}
        return out

    # ------------------------------------------------------------------
    # live view (any thread)
    # ------------------------------------------------------------------
    def session_info(self, session_id: str) -> dict | None:
        s = self._sessions.get(session_id)
        if s is None:
            return None
        return {
            "session_id": s.session_id, "user_id": s.user_id,
            "state": s.state, "challenge": s.challenge,
            "url": s.url, "title": s.title, "frame_seq": s.frame_seq,
            "viewport": dict(VIEWPORT), "cursor": s.cursor,
            "last_action": s.log[-1] if s.log else None,
            "log": s.log[-40:],
            "pending_commit": bool(s.pending_commit),
            "controller": s.controller,
            "downloads": list(s.downloads[-10:]),
        }

    def frame(self, session_id: str) -> tuple[bytes, int]:
        s = self._sessions.get(session_id)
        if s is None:
            raise BrowserError("UNKNOWN_SESSION", "no such session")
        return s.frame, s.frame_seq

    def user_input(self, session_id: str, event: dict) -> dict:
        """The user takes over from the live view: click / type / key / scroll."""
        def run():
            s = self._get(session_id)
            kind = event.get("kind")
            page = s.page
            if kind in ("take_control", "release_control"):
                s.controller = "user" if kind == "take_control" else "agent"
                self._log(s, "You took control" if s.controller == "user" else "Handed back to OpenMuse",
                          actor="user", kind="control")
                return self.session_info(session_id)
            s.user_touched = True
            if kind == "click":
                x, y = float(event["x"]), float(event["y"])
                s.cursor = {"x": x, "y": y, "actor": "user", "at": time.time()}
                page.mouse.click(x, y)
                self._log(s, "You clicked", actor="user", kind="click")
            elif kind == "type":
                page.keyboard.type(str(event.get("text", ""))[:500], delay=15)
                self._log(s, "You typed", actor="user", kind="type")  # value not logged
            elif kind == "key":
                key = str(event.get("key", ""))
                if key not in ("Enter", "Tab", "Backspace", "Escape", "ArrowUp", "ArrowDown",
                               "ArrowLeft", "ArrowRight", "Space", "Delete"):
                    raise BrowserError("BAD_ARGUMENT", "unsupported key")
                page.keyboard.press(" " if key == "Space" else key)
            elif kind == "scroll":
                page.mouse.wheel(0, max(-2000, min(2000, int(event.get("dy", 400)))))
            elif kind == "navigate":
                url = str(event.get("url", ""))
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                self._log(s, f"You opened {_host(url)}", actor="user", kind="navigate")
            else:
                raise BrowserError("BAD_ARGUMENT", f"unknown input kind {kind!r}")
            page.wait_for_timeout(400)
            if s.state == "challenged":
                self._observe(s)  # re-check: user may have cleared the challenge
            self._capture(s)
            return self.session_info(session_id)
        return self._call(run)

    def shutdown(self) -> None:
        def run():
            for s in self._sessions.values():
                if s.state != "closed":
                    self._close(s)
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        try:
            self._call(run, timeout=20)
        finally:
            self._stopped = True


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.netloc else url


def _host(url: str) -> str:
    return urlparse(url).netloc or url


def _label_of(loc) -> str:
    try:
        txt = loc.evaluate(
            "e => (e.getAttribute('aria-label') || e.innerText || e.value || "
            "e.getAttribute('placeholder') || e.getAttribute('title') || '').trim()")
        return re.sub(r"\s+", " ", txt or "")[:80]
    except Exception:
        return ""


def _find_amount(page) -> int:
    """Best-effort visible total (minor units) for the commit card."""
    try:
        text = page.evaluate("() => document.body.innerText.slice(0, 20000)")
    except Exception:
        return 0
    m = re.findall(r"(?:total|due|pay)[^$\n]{0,40}\$\s?([\d,]+(?:\.\d{2})?)", text, re.I)
    if not m:
        return 0
    try:
        return int(round(float(m[-1].replace(",", "")) * 100))
    except ValueError:
        return 0

