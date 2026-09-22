"""Prompt-injection classifier + deterministic destination checks (Phase 10).

The classifier is purely deterministic (compiled regex rules, no LLM): it
flags injection attempts in untrusted content with a severity of
"none" | "suspect" | "block". Block-severity findings force DENY in the
policy engine regardless of approvals.

Destination checks are equally deterministic: the destination of an action
(URL host, message recipient, file path) must match the user's authorized
intent. A cross-origin browser redirect, for example, triggers
re-evaluation instead of silently continuing.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlparse


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"\s+", " ", text)
    return text


# (rule_id, severity, description, pattern)
_RULES: list[tuple[str, str, str, str]] = [
    ("override_direct", "block",
     "Direct instruction override",
     r"(?i)\b(ignore|disregard|forget|override)\s+(all\s+|your\s+|previous\s+|these\s+)*instructions\b"),
    ("override_system", "block",
     "System-prompt override attempt",
     r"(?i)\b(system\s+prompt|your\s+core\s+instructions)\s+(say|state|instruct|order)[s]?\b.*\b(ignore|disregard)\b"),
    ("role_reassign", "block",
     "Role reassignment",
     r"(?i)\b(you\s+are\s+now|your\s+new\s+role\s+is|act\s+as\s+an?\s+(attacker|hacker|unrestricted|evil)|pretend\s+(you\s+are|to\s+be)\s+(not|no\s+longer))"),
    ("delimiter_smuggle", "block",
     "Forged system/role delimiters",
     r"(\[SYSTEM\]|\[INST\]|<<SYS>>|###\s*SYSTEM|<(system|assistant)>)"),
    ("exfil_directive", "block",
     "Data-exfiltration directive",
     r"(?i)\b(send|forward|exfiltrate|upload|post)\b.{0,60}\b(to|at)\b.{0,40}(attacker|evil|malicious|external|https?://[^\s]*\.(ru|cn|tk|xyz)\b)"),
    ("tool_smuggle", "block",
     "Smuggled tool-call syntax",
     r"(\{\s*\"tool\"\s*:|\"function\s*call\"|tool_call\s*\(|<tool_code>)"),
    ("jailbreak_phrase", "block",
     "Known jailbreak phrasing",
     r"(?i)\b(do\s+anything\s+now|DAN\s+mode|jailbreak|bypass\s+(your|the)\s+safety|unfiltered\s+mode)\b"),
    ("authority_claim", "suspect",
     "False authority claim",
     r"(?i)\b(as\s+an?\s+(admin|administrator|developer|system\s+operator)|admin(istrator)?\s+override|i\s+am\s+(the\s+)?(developer|system))\b"),
    ("user_ventriloquism", "suspect",
     "Forged user instruction inside untrusted content",
     r"(?i)\b(the\s+user\s+(said|told|asked|instructed)\s+(you|me)\s+to)\b"),
    ("instruction_header", "suspect",
     "Instruction header inside data",
     r"(?m)^\s*(Instruction|Command|Task|Action\s+required)\s*:"),
    ("urgency_coercion", "suspect",
     "Urgency coercion paired with an action verb",
     r"(?i)\b(immediately|urgent(ly)?|asap|right\s+now|without\s+asking)\b.{0,40}\b(send|transfer|delete|pay|execute|run)\b"),
    ("encoding_trick", "suspect",
     "Encoded blob presented as instructions",
     r"(?i)\b(decode|decrypt|base64)\b.{0,40}[A-Za-z0-9+/]{40,}={0,2}"),
]

_COMPILED = [(rid, sev, desc, re.compile(pat)) for rid, sev, desc, pat in _RULES]


@dataclass
class InjectionFinding:
    severity: str  # none | suspect | block
    matched_rules: tuple[str, ...] = ()
    rule_descriptions: tuple[str, ...] = ()
    source_kind: str = ""

    @property
    def blocked(self) -> bool:
        return self.severity == "block"


class InjectionClassifier:
    """Deterministic prompt-injection classifier. No model calls."""

    def __init__(self, rules: list[tuple] | None = None):
        self._rules = rules if rules is not None else _COMPILED

    def classify(self, text: str, *, source_kind: str = "") -> InjectionFinding:
        norm = _normalize(text)
        matched: list[tuple[str, str]] = []
        for rid, sev, desc, pat in self._rules:
            if pat.search(norm):
                matched.append((rid, sev, desc))
        if any(sev == "block" for _, sev, _ in matched):
            severity = "block"
        elif matched:
            # Any suspect rule matched: flag for risk escalation. The
            # policy engine raises the call's risk class (never lowers it).
            severity = "suspect"
        else:
            severity = "none"
        return InjectionFinding(
            severity=severity,
            matched_rules=tuple(r for r, _, _ in matched),
            rule_descriptions=tuple(d for _, _, d in matched),
            source_kind=source_kind,
        )


def classify_text(text: str, *, source_kind: str = "") -> InjectionFinding:
    return InjectionClassifier().classify(text, source_kind=source_kind)


# ---------------------------------------------------------------------------
# Deterministic destination checks
# ---------------------------------------------------------------------------

@dataclass
class DestinationCheck:
    ok: bool
    reason_code: str = ""       # DESTINATION_OK | CROSS_ORIGIN_REDIRECT | ...
    detail: str = ""
    expected: str = ""
    actual: str = ""


def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def check_destination(
    tool_name: str,
    arguments: dict,
    *,
    intent: dict,
) -> DestinationCheck:
    """Verify the action's destination matches the authorized intent.

    intent keys (all optional; absent keys are not checked):
      allowed_origins: set[str] — browser navigation targets
      session_origin: str — the origin the browser session is bound to
      authorized_recipients: set[str] — external message recipients
      authorized_roots: list[str] — filesystem roots for writes
    """
    # -- browser navigation / actions carrying a URL -----------------------
    url = arguments.get("url") or ""
    if tool_name in ("browser.navigate", "browser.act") and url:
        host = _host(url).lower()
        allowed = {h.lower() for h in intent.get("allowed_origins", set())}
        session_origin = (intent.get("session_origin") or "").lower()
        if host and (host in allowed or (session_origin and host == session_origin)):
            return DestinationCheck(ok=True, reason_code="DESTINATION_OK", actual=host)
        return DestinationCheck(
            ok=False, reason_code="DESTINATION_MISMATCH",
            detail=f"navigation target {host!r} is outside the authorized intent",
            expected="|".join(sorted(allowed | ({session_origin} if session_origin else set()))),
            actual=host,
        )

    # -- cross-origin redirect re-evaluation --------------------------------
    if tool_name == "browser.redirect_reevaluate":
        prev = (arguments.get("from_origin") or "").lower()
        new = (arguments.get("to_origin") or "").lower()
        allowed = {h.lower() for h in intent.get("allowed_origins", set())}
        if new and new != prev and new not in allowed:
            return DestinationCheck(
                ok=False, reason_code="CROSS_ORIGIN_REDIRECT",
                detail=f"redirect {prev!r} -> {new!r} leaves the authorized origin set; re-evaluation required",
                expected="|".join(sorted(allowed)), actual=new,
            )
        return DestinationCheck(ok=True, reason_code="DESTINATION_OK", actual=new)

    # -- external message recipient -----------------------------------------
    recipient = arguments.get("recipient") or arguments.get("to") or ""
    if tool_name in ("production.channel_send",) and recipient:
        authorized = set(intent.get("authorized_recipients", set()))
        if authorized and recipient not in authorized:
            return DestinationCheck(
                ok=False, reason_code="RECIPIENT_MISMATCH",
                detail=f"recipient {recipient!r} not in authorized recipients",
                expected="|".join(sorted(authorized)), actual=recipient,
            )
        return DestinationCheck(ok=True, reason_code="DESTINATION_OK", actual=recipient)

    # -- filesystem writes stay inside authorized roots ----------------------
    path = arguments.get("path") or ""
    if tool_name in ("files.write",) and path:
        roots = intent.get("authorized_roots", [])
        norm = unicodedata.normalize("NFKC", path)
        if roots and not any(
            norm == r or norm.startswith(r.rstrip("/") + "/") for r in roots
        ):
            return DestinationCheck(
                ok=False, reason_code="PATH_ESCAPE",
                detail=f"write path {path!r} escapes authorized roots",
                expected="|".join(roots), actual=path,
            )
        return DestinationCheck(ok=True, reason_code="DESTINATION_OK", actual=path)

    return DestinationCheck(ok=True, reason_code="DESTINATION_OK")
