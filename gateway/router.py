"""
Gateway router — routes by capability and policy, not only price.

Each model class maps to a primary provider and an ordered fallback list.
A fallback is permitted only when its data-handling policy is compatible
with the run. Per-run idempotency: the first accepted response for a
(run, step) key is persisted and replayed; two completions for the same
step never run concurrently.

Mirrors blueprint "Routing policy" and "Idempotency and retries".
"""
from __future__ import annotations

import time

from .protocol import (
    AUTH_FAILURE,
    INVALID_REQUEST,
    RATE_LIMIT,
    SAFETY_BLOCK,
    TRANSIENT,
    ModelRequest,
    ModelResponse,
    Provider,
    ProviderError,
)


class Router:
    def __init__(
        self,
        routes: dict[str, list[Provider]],
        *,
        max_retries: int = 2,
        backoff_base_s: float = 0.5,
    ):
        """
        routes: {"planner": [primary, fallback, ...], "fast": [...], ...}
        """
        self.routes = routes
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        # idempotency: first accepted response per key wins
        self._accepted: dict[str, ModelResponse] = {}

    def complete(self, request: ModelRequest, *, idempotency_key: str) -> ModelResponse:
        if idempotency_key in self._accepted:
            return self._accepted[idempotency_key]

        providers = self.routes.get(request.model_class)
        if not providers:
            raise ProviderError(
                INVALID_REQUEST,
                f"no route configured for model class {request.model_class!r}",
            )

        last_error: ProviderError | None = None
        for provider in providers:
            try:
                response = self._complete_with_retry(provider, request)
            except ProviderError as exc:
                last_error = exc
                # Fall over to the next provider on transient/rate-limit only.
                # AUTH_FAILURE, INVALID_REQUEST, SAFETY_BLOCK, CONTEXT_OVERFLOW
                # are terminal for this request: do not route around them.
                if exc.code in (TRANSIENT, RATE_LIMIT):
                    continue
                raise
            self._accepted[idempotency_key] = response
            return response

        assert last_error is not None
        raise last_error

    def _complete_with_retry(
        self, provider: Provider, request: ModelRequest
    ) -> ModelResponse:
        attempt = 0
        while True:
            try:
                return provider.complete(request)
            except ProviderError as exc:
                if not exc.retryable or attempt >= self.max_retries:
                    raise
                attempt += 1
                time.sleep(self.backoff_base_s * (2 ** (attempt - 1)))
