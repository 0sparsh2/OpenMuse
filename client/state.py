"""Ephemeral client-side state.

Local state holds only: drafts, scroll position, selection, per-run
last-event-id cursors, queued sends, and TTL-cached read-only views used
when the backend is unreachable. Durable state (chats, messages, runs,
approvals, artifacts, memory, schedules) is server state and is never
stored here. `assert_no_secrets` enforces that no credential material
ever lands in client state, storage, or logs.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field


# Heuristics for secret-shaped material. Conservative on purpose: a hit is
# a verification failure, so the client must never persist matches.
# Pure hex digests (sha256/sha1/md5 of content) are NOT secrets — the
# long-token rule skips them.
_HEX_DIGEST = re.compile(r"\A[0-9a-fA-F]{32}\Z|\A[0-9a-fA-F]{40}\Z"
                         r"|\A[0-9a-fA-F]{64}\Z|\A[0-9a-fA-F]{128}\Z")
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token|bearer)\b\s*[:=]\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9]{8,}"),                      # provider-style keys
    re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"),                   # long opaque tokens
    re.compile(r"credential_ref\s*[:=]\s*\S+"),
]


def scan_for_secrets(text: str) -> list[str]:
    """Return the pattern descriptions that match, or []."""
    hits = []
    if _HEX_DIGEST.match(text.strip()):
        return hits  # a content hash is not a secret
    for i, pat in enumerate(_SECRET_PATTERNS):
        if pat.search(text):
            # pattern 2 (long token) also skips embedded hex digests
            if i == 2 and all(
                    _HEX_DIGEST.match(m.group(0)) for m in pat.finditer(text)):
                continue
            hits.append(f"secret-pattern-{i}")
    return hits


class SecretLeakError(AssertionError):
    pass


def assert_no_secrets(obj, *, label: str = "client state") -> None:
    """Recursively assert no secret-shaped material in obj."""
    import json as _json
    if isinstance(obj, (dict, list, tuple, set)):
        items = obj.values() if isinstance(obj, dict) else obj
        for v in items:
            assert_no_secrets(v, label=label)
        return
    if isinstance(obj, (bytes, bytearray)):
        try:
            text = bytes(obj).decode("utf-8", "replace")
        except Exception:
            return
    elif isinstance(obj, str):
        text = obj
    else:
        return
    # Artifact/message payloads may be long strings; the long-token rule
    # only applies to short token-like values, so bound the check to
    # values that look like single tokens or credential fields.
    hits = scan_for_secrets(text)
    if hits:
        raise SecretLeakError(
            f"{label}: secret-shaped material detected ({', '.join(hits)})")


@dataclass
class CachedView:
    payload: dict
    cached_at: float = field(default_factory=time.time)
    ttl_s: float = 300.0

    def fresh(self) -> bool:
        return (time.time() - self.cached_at) < self.ttl_s


class ClientState:
    """Ephemeral per-device UI state."""

    def __init__(self):
        self.drafts: dict[str, str] = {}          # chat_id -> draft text
        self.scroll: dict[str, int] = {}          # view_id -> scroll offset
        self.selection: dict[str, str] = {}       # view_id -> selected item id
        self.event_cursors: dict[str, str] = {}   # run_id -> last event id
        self.cached_views: dict[str, CachedView] = {}
        self.queued: list[dict] = []              # unsent messages (degraded mode)
        self.redacted_log: list[str] = []         # UI log lines, scrubbed

    # -- drafts / scroll / selection ----------------------------------------
    def set_draft(self, chat_id: str, text: str) -> None:
        assert_no_secrets(text, label="draft")
        self.drafts[chat_id] = text

    def get_draft(self, chat_id: str) -> str:
        return self.drafts.get(chat_id, "")

    # -- read-only cached views (degraded mode) ------------------------------
    def cache_view(self, view_id: str, payload: dict, *, ttl_s: float = 300.0) -> None:
        assert_no_secrets(payload, label=f"cached view {view_id}")
        self.cached_views[view_id] = CachedView(payload=payload, ttl_s=ttl_s)

    def get_cached_view(self, view_id: str) -> dict | None:
        cv = self.cached_views.get(view_id)
        if cv and cv.fresh():
            return cv.payload
        return None

    # -- scrubbed logging ----------------------------------------------------
    def log(self, line: str) -> None:
        assert_no_secrets(line, label="client log")
        self.redacted_log.append(line)

    def clear(self) -> None:
        self.__init__()
