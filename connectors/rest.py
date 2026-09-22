"""Generic REST connector base with the provider error taxonomy.

A RESTConnector owns: base URL, auth header construction, pagination helpers,
and mapping of HTTP failures into ProviderError codes — including the
terminal rate-limit signal that triggers the registry's hard stop.

The transport is injectable so adapters run fully offline against
MockTransport in tests and demos.
"""
from __future__ import annotations

import abc
import json

from connectors.models import (
    ConnectorManifest, ProviderError, ProviderErrorCode as C,
)


class Transport(abc.ABC):
    @abc.abstractmethod
    def request(self, *, method: str, url: str, headers: dict,
                params: dict | None, body: dict | None) -> "HTTPResponse":
        raise NotImplementedError


class HTTPResponse:
    def __init__(self, status: int, body: dict, headers: dict | None = None):
        self.status = status
        self.body = body
        self.headers = headers or {}


class MockTransport(Transport):
    """Canned-route transport for offline tests. Routes are (method, path)
    -> handler(params, body, headers) returning HTTPResponse. A scenario hook
    can prime failure modes (rate limits, auth failures, echo attacks)."""

    def __init__(self):
        self._routes: dict[tuple[str, str], object] = {}
        self.calls: list[dict] = []

    def add_route(self, method: str, path: str, handler) -> None:
        self._routes[(method.upper(), path)] = handler

    def request(self, *, method: str, url: str, headers: dict,
                params: dict | None, body: dict | None) -> HTTPResponse:
        self.calls.append({"method": method, "url": url,
                           "params": params, "body": body,
                           "headers": dict(headers)})
        path = url.split("?", 1)[0]
        # strip scheme+host so routes are registered as "/repos/..."
        if "://" in path:
            path = "/" + path.split("://", 1)[1].split("/", 1)[1]
        handler = self._routes.get((method.upper(), path))
        if handler is None:
            return HTTPResponse(404, {"message": "not found"})
        return handler(params or {}, body or {}, headers)


class RESTConnector(abc.ABC):
    manifest: ConnectorManifest
    base_url: str = ""

    def __init__(self, transport: Transport):
        self.transport = transport

    # -- implemented by the concrete connector --------------------------------
    @abc.abstractmethod
    def auth_headers(self, credential: str) -> dict:
        """Build auth headers from the raw credential. Called with the
        short-lived handle only; the result never leaves this adapter."""

    @abc.abstractmethod
    def execute(self, op_name: str, credential: str, args: dict) -> dict:
        """Dispatch one declared operation."""

    # -- shared machinery -------------------------------------------------------
    def _call(self, *, credential: str, method: str, path: str,
              params: dict | None = None, body: dict | None = None) -> dict:
        resp = self.transport.request(
            method=method, url=self.base_url + path,
            headers=self.auth_headers(credential), params=params, body=body)
        return self._classify(resp, method=method, path=path)

    @staticmethod
    def _classify(resp: HTTPResponse, *, method: str, path: str) -> dict:
        s = resp.status
        if 200 <= s < 300:
            return resp.body
        if s == 401:
            raise ProviderError(C.AUTH_FAILURE,
                                "The provider rejected the credential.",
                                retryable=False)
        if s == 403 and "rate limit" in json.dumps(resp.body).lower():
            raise ProviderError(C.RATE_LIMIT_TERMINAL,
                                "The provider rate-limited this account.",
                                retryable=False)
        if s == 403:
            raise ProviderError(C.FORBIDDEN,
                                "The provider refused this operation.",
                                retryable=False)
        if s == 404:
            raise ProviderError(C.NOT_FOUND,
                                "The provider has no such resource.",
                                retryable=False)
        if s == 429:
            remaining = resp.headers.get("x-ratelimit-remaining")
            if remaining == "0":
                raise ProviderError(C.RATE_LIMIT_TERMINAL,
                                    "The provider rate-limited this account.",
                                    retryable=False)
            raise ProviderError(C.RATE_LIMIT,
                                "The provider is throttling requests.",
                                retryable=True)
        if 400 <= s < 500:
            raise ProviderError(C.INVALID_REQUEST,
                                "The request was invalid.",
                                retryable=False)
        raise ProviderError(C.TRANSIENT,
                            "The provider had a transient error.",
                            retryable=True)

    def paginate(self, *, credential: str, method: str, path: str,
                 params: dict | None = None, page_key: str = "page",
                 per_page: int = 50, max_pages: int = 10) -> list:
        """Deterministic pagination with a page cap (no unbounded walks)."""
        out: list = []
        for page in range(1, max_pages + 1):
            p = dict(params or {})
            p[page_key] = page
            p["per_page"] = per_page
            body = self._call(credential=credential, method=method,
                              path=path, params=p)
            items = body.get("items", body if isinstance(body, list) else [])
            out.extend(items)
            if len(items) < per_page:
                break
        return out
