"""
Jev (TypeSafe AI) decision client — fast, typed decisions with calibrated
probabilities: noul (yes/no), choice, score. Used for narrow judgements inside
flows the main model still leads (which results to open, "is this enough?",
"does this turn need a search?", page types in computer use).

Every caller has a fallback: no key, a timeout or an error returns None and
the caller uses embeddings, rules or the main model instead — Jev being
down makes things slower, never broken. Nothing private is sent unless the
caller puts it in `state`.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time

import requests

URL = "https://api.typesafe.ai/v1/systemone"


class Jev:
    def __init__(self, api_key: str | None = None, *, model: str = "jev-latest", timeout: float = 2.5,
                 cache_seconds: int = 600):
        self.api_key = api_key if api_key is not None else os.environ.get("JEV_API_KEY", "")
        self.model = model
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self._fail_until = 0.0            # simple circuit breaker
        self.calls = 0
        self.failures = 0

    @property
    def available(self) -> bool:
        return bool(self.api_key) and time.time() >= self._fail_until

    def decide(self, state, questions: dict, *, timeout: float | None = None) -> dict | None:
        """{question_key: answer} or None. Answers: noul -> {"noul": p};
        choice -> {"choice", "probabilities", "confidence"}; score -> {"score", ...}."""
        if not self.available or not questions:
            return None
        key = hashlib.sha256(json.dumps([state, questions], sort_keys=True, default=str).encode()).hexdigest()
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < self.cache_seconds:
                return hit[1]
        try:
            self.calls += 1
            r = requests.post(URL, timeout=timeout or self.timeout,
                              headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
                              json={"model": self.model, "state": state, "questions": questions})
            if r.status_code == 401:
                self._fail_until = time.time() + 3600
                return None
            if r.status_code >= 500 or r.status_code == 429:
                raise RuntimeError(f"jev {r.status_code}")
            if r.status_code != 200:
                return None
            answers = r.json().get("answers") or {}
        except Exception:
            self.failures += 1
            self._fail_until = time.time() + 30   # back off briefly; callers fall back
            return None
        with self._lock:
            if len(self._cache) > 2000:
                self._cache.clear()
            self._cache[key] = (time.time(), answers)
        return answers

    # -- helpers --------------------------------------------------------------------------
    def noul(self, state, question: str, **kw) -> float | None:
        a = self.decide(state, {"q": {"type": "noul", "instructions": question}}, **kw)
        return None if a is None else float(a.get("q", {}).get("noul", 0.0))

    def scores(self, state, questions: dict[str, str], levels: list[str], **kw) -> dict[str, float] | None:
        """Several score questions over one state -> {key: score in [0, len(levels)-1]}."""
        a = self.decide(state, {k: {"type": "score", "instructions": q, "criteria": levels}
                                for k, q in questions.items()}, **kw)
        return None if a is None else {k: float(v.get("score", 0.0)) for k, v in a.items()}

    def choice(self, state, question: str, options: dict[str, str], **kw) -> tuple[str, dict] | None:
        a = self.decide(state, {"q": {"type": "choice", "instructions": question, "criteria": options}}, **kw)
        if a is None or "q" not in a:
            return None
        return a["q"].get("choice", ""), a["q"].get("probabilities", {})


_DEFAULT: Jev | None = None


def default_jev() -> Jev:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Jev()
    return _DEFAULT
