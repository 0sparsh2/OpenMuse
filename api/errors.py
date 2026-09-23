"""
Machine-readable error taxonomy for the external API.

Every error response is {"error": {"code", "message", "request_id", "details"}}.
Codes are stable across /v1; messages are human-readable and never leak
secrets. Internal platform codes (policy denials, provider failures, run
failure codes like WALL_BUDGET) are surfaced inside run event payloads, not
as HTTP errors, unless the request itself is invalid.
"""
from __future__ import annotations

# (code, http_status, default message)
INVALID_API_KEY = ("INVALID_API_KEY", 401, "The API key is invalid or revoked.")
MISSING_API_KEY = ("MISSING_API_KEY", 401, "Missing Authorization: Bearer <api-key> header.")
INSUFFICIENT_SCOPE = ("INSUFFICIENT_SCOPE", 403, "The API key lacks the required scope for this endpoint.")
VALIDATION_ERROR = ("VALIDATION_ERROR", 400, "The request body failed validation.")
NOT_FOUND = ("NOT_FOUND", 404, "The requested resource does not exist.")
METHOD_NOT_ALLOWED = ("METHOD_NOT_ALLOWED", 405, "The HTTP method is not supported for this path.")
RATE_LIMITED = ("RATE_LIMITED", 429, "Rate limit exceeded. Retry after the Retry-After interval.")
IDEMPOTENCY_KEY_REUSED = (
    "IDEMPOTENCY_KEY_REUSED",
    422,
    "This Idempotency-Key was already used with a different request body.",
)
RUN_ALREADY_TERMINAL = ("RUN_ALREADY_TERMINAL", 409, "The run is already in a terminal state.")
APPROVAL_NOT_PENDING = ("APPROVAL_NOT_PENDING", 409, "The approval request is no longer pending.")
APPROVAL_HASH_MISMATCH = (
    "APPROVAL_HASH_MISMATCH",
    409,
    "The argument_hash does not match the parked approval request.",
)
APPROVAL_EXPIRED = ("APPROVAL_EXPIRED", 410, "The approval request has expired.")
RUN_NOT_FAILED = ("RUN_NOT_FAILED", 409, "The run did not fail in a way that permits retry.")
CONFIRM_ON_SCREEN = ("CONFIRM_ON_SCREEN", 403, "This action needs your confirmation on screen, not by voice.")
INTERNAL_ERROR = ("INTERNAL_ERROR", 500, "An unexpected server error occurred.")
SERVICE_UNAVAILABLE = ("SERVICE_UNAVAILABLE", 503, "The service is temporarily unavailable.")


def envelope(code: str, message: str, request_id: str, details: dict | None = None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "details": details or {},
        }
    }


# Internal platform failure codes that may appear inside run.failed events.
# They are data, not HTTP errors; clients match on them for programmatic handling.
RUN_FAILURE_CODES = frozenset({
    "WALL_BUDGET", "MAX_MODEL_CALLS", "MAX_TOOL_CALLS", "EMPTY_FINAL",
    "PROVIDER_RATE_LIMIT", "PROVIDER_AUTH", "PROVIDER_UNAVAILABLE",
    "UNKNOWN_OUTCOME",
})
