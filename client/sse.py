"""SSE consumption with Last-Event-ID resume.

Mirrors the /v1/runs/{run_id}/events contract: named events with numeric
ids (run.status, assistant.delta, approval.required, run.completed, ...).
"""
from __future__ import annotations

import http.client
import json
import time
from dataclasses import dataclass, field


@dataclass
class SSEEvent:
    id: str            # server-sent event id (numeric string)
    name: str          # event name, e.g. "assistant.delta"
    data: dict         # parsed JSON payload
    raw: str = ""      # raw data line, kept for debugging (never secrets)


def parse_sse_block(block: bytes) -> SSEEvent | None:
    cur: dict[str, str] = {}
    for line in block.decode("utf-8", "replace").split("\n"):
        if line.startswith("id:"):
            cur["id"] = line[3:].strip()
        elif line.startswith("event:"):
            cur["event"] = line[6:].strip()
        elif line.startswith("data:"):
            cur["data"] = cur.get("data", "") + line[5:].strip()
    if "event" not in cur:
        return None
    data = cur.get("data", "")
    try:
        payload = json.loads(data) if data else {}
    except ValueError:
        payload = {"_raw": data}
    return SSEEvent(id=cur.get("id", ""), name=cur["event"],
                    data=payload, raw=data)


def stream_events(base: str, path: str, *, key: str | None = None,
                  last_event_id: str | None = None,
                  stop_when=None, timeout: float = 25.0) -> list[SSEEvent]:
    """Read SSE until stop_when(event) is true, the stream ends, or timeout."""
    host = base.split("://", 1)[1]
    conn = http.client.HTTPConnection(host, timeout=timeout)
    headers = {"Accept": "text/event-stream"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if last_event_id is not None:
        headers["Last-Event-ID"] = str(last_event_id)
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    if resp.status != 200:
        body = resp.read()
        conn.close()
        from .http import HttpError
        raise HttpError(resp.status, {"_raw": body}, dict(resp.getheaders()))
    events: list[SSEEvent] = []
    buf = b""
    read1 = getattr(resp, "read1", None)
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            try:
                chunk = read1(65536) if read1 else resp.read(65536)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                block, buf = buf.split(b"\n\n", 1)
                ev = parse_sse_block(block)
                if ev is not None:
                    events.append(ev)
                    if stop_when and stop_when(ev):
                        return events
    finally:
        conn.close()
    return events


class SSEClient:
    """Resumable run-event stream.

    Keeps the last seen event id; reconnect() resumes exactly where the
    previous read stopped (server replays only missed events).
    """

    def __init__(self, base: str, key: str | None = None):
        self.base = base
        self.key = key
        self.last_event_id: str | None = None

    def read_run(self, run_id: str, *, stop_when=None,
                 timeout: float = 25.0) -> list[SSEEvent]:
        events = stream_events(
            self.base, f"/v1/runs/{run_id}/events", key=self.key,
            last_event_id=self.last_event_id, stop_when=stop_when,
            timeout=timeout)
        for ev in events:
            if ev.id:
                self.last_event_id = ev.id
        return events

    def reconnect(self, run_id: str, *, stop_when=None,
                  timeout: float = 25.0) -> list[SSEEvent]:
        """Resume the stream from the last seen event id (no duplicates)."""
        return self.read_run(run_id, stop_when=stop_when, timeout=timeout)
