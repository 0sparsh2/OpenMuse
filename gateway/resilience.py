"""
Resilient model calls for hosted endpoints (NVIDIA NIM).

Hosted models fail in a few recurring ways; each has a remedy:
  - transient errors (5xx, 429, errors inside a stream, dropped streams):
    retry with backoff, then switch to a fallback model;
  - an empty reply (a reasoning model can spend its whole budget thinking on
    a long tool result and write nothing): ask once more with thinking off,
    instead of failing the turn;
  - voice turns go straight to the no-thinking model (latency).
"""
from __future__ import annotations

import time

from gateway.protocol import ProviderError


class Resilient:
    def __init__(self, primary, *, fallback=None, no_think=None, attempts=(4, 3), sleep=time.sleep, log=print):
        self.primary, self.fallback, self.no_think = primary, fallback, no_think
        self.attempts, self.sleep, self.log = attempts, sleep, log

    def complete(self, request, primary=None):
        last = None
        chain = [(primary or self.primary, self.attempts[0])]
        if self.fallback is not None:
            chain.append((self.fallback, self.attempts[1]))
        for prov, attempts in chain:
            for attempt in range(attempts):
                try:
                    resp = prov.complete(request)
                    if prov is self.fallback:
                        self.log(f"answered by fallback model {prov.model}")
                    return resp
                except ProviderError as exc:
                    last = exc
                    if exc.code == "CANCELLED":
                        raise           # the user stopped it: no retry, no fallback model
                    if not exc.retryable:
                        break  # auth/invalid request: the same model won't do better
                    wait = min(2 ** attempt, 8)
                    self.log(f"{getattr(prov, 'model', 'model')}: {exc.code}; retrying in {wait}s")
                    self.sleep(wait)
        raise last

    def respond(self, request, history=None):
        voice = request.metadata is not None and getattr(request.metadata, "mode", "") == "voice"
        resp = self.complete(request, primary=self.no_think if (voice and self.no_think) else None)
        if not (resp.text or "").strip() and not resp.tool_calls and self.no_think is not None and not voice:
            self.log(f"{getattr(self.primary, 'model', 'model')}: empty reply ({resp.stop_reason or 'no reason'}); "
                     f"retrying with thinking off")
            resp = self.complete(request, primary=self.no_think)
        return resp
