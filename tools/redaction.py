"""
Secret redaction — safety basics, Phase 1.

No raw secret may reach the model in tool outputs, messages, files, logs, or
memory. `redact_text` scans for secret-shaped values and replaces them with
labeled placeholders. Callers should ALSO avoid placing secrets in tool
arguments (the gateway prefers vault references; Phase 6 builds the vault).
"""
from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("api_key", re.compile(r"\bsk-(proj|live|test)-[A-Za-z0-9_-]{10,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[bpras]-[A-Za-z0-9-]{10,}\b")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/=]{20,}\b")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|passwd|password|auth[_-]?token|access[_-]?token|refresh[_-]?token|client[_-]?secret)\b\s*[:=]\s*['\"]?([^\s'\",;]{6,})['\"]?"
        ),
    ),
]


def redact_text(text: str) -> tuple[str, list[str]]:
    """
    Returns (redacted_text, [labels...]). Labels name the *kind* of secret
    found, never its value.
    """
    found: list[str] = []
    out = text

    def _sub(label: str, pattern: re.Pattern, s: str) -> str:
        def repl(m: re.Match) -> str:
            found.append(label)
            # credential_assignment has the value in group 2; keep the key name.
            if m.lastindex and m.lastindex >= 2:
                return f"{m.group(1)}=[REDACTED:{label}]"
            return f"[REDACTED:{label}]"

        return pattern.sub(repl, s)

    for label, pattern in _PATTERNS:
        out = _sub(label, pattern, out)
    return out, sorted(set(found))


def looks_like_secret(text: str) -> bool:
    """Cheap pre-check used by tools before persisting or echoing values."""
    redacted, found = redact_text(text)
    return bool(found) and redacted != text
