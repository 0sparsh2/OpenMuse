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
