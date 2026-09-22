"""Low-level HTTP helper for the client layer (stdlib only)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class HttpError(Exception):
    def __init__(self, status: int, body: dict, headers: dict):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body
        self.headers = headers


def api_request(base: str, method: str, path: str, *, key: str | None = None,
                body: dict | None = None, headers: dict | None = None,
                raw_body: bytes | None = None,
                content_type: str = "application/json",
                timeout: float = 30.0) -> tuple[int, dict, dict]:
    """Issue one HTTP request. Returns (status, parsed_body, headers).

    parsed_body is {} for empty bodies and {"_raw": bytes} for
    non-JSON payloads (e.g. artifact downloads).
    """
    h = dict(headers or {})
    if key:
        h["Authorization"] = f"Bearer {key}"
    data: bytes | None = raw_body
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = content_type
    req = urllib.request.Request(base + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, _parse(raw), dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            payload = {"_raw": raw}
        raise HttpError(e.code, payload, dict(e.headers))


def _parse(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return {"_raw": raw}
