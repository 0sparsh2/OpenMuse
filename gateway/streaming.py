"""
Live token sinks (voice mode, issue #19).

A run can ask for its model output as it's generated: the backend registers
a sink for the run id, and providers that support streaming call it with
each text delta (and the step it belongs to). Requests stay plain data —
the callback lives here, keyed by run id, never inside the request.
"""
from __future__ import annotations

import threading
from typing import Callable

_sinks: dict[str, Callable[[str, int], None]] = {}
_lock = threading.Lock()


def register(run_id: str, fn: Callable[[str, int], None]) -> None:
    with _lock:
        _sinks[run_id] = fn


def unregister(run_id: str) -> None:
    with _lock:
        _sinks.pop(run_id, None)


def sink_for(run_id: str | None):
    if not run_id:
        return None
    with _lock:
        return _sinks.get(run_id)


class PartialText:
    """Coalesces streamed tokens into chunks (~every 60 chars, 120 ms, or a line
    break) and hands them to `emit(text, step)`; `emit(None, step)` = reset."""

    def __init__(self, emit, *, min_chars: int = 60, max_wait: float = 0.12, cancelled=None):
        import time as _t
        self._t = _t
        self.emit = emit
        if cancelled is not None:
            self.cancelled = cancelled   # providers poll this and end the stream on Stop
        self.min_chars, self.max_wait = min_chars, max_wait
        self.buf, self.step, self.last = "", None, 0.0
        self.lock = threading.Lock()

    def _flush(self):
        if self.buf:
            text, self.buf = self.buf, ""
            self.last = self._t.monotonic()
            self.emit(text, self.step)

    def start(self, step: int) -> None:
        with self.lock:
            self.buf, self.step = "", step
            self.last = self._t.monotonic()
            self.emit(None, step)

    def __call__(self, text: str, step: int) -> None:
        with self.lock:
            if step != self.step:
                self._flush()
                self.step = step
            self.buf += text or ""
            if (len(self.buf) >= self.min_chars or "\n" in text
                    or self._t.monotonic() - self.last >= self.max_wait):
                self._flush()

    def end(self, step: int) -> None:
        with self.lock:
            self._flush()
