"""
OpenAPI 3.0 document for the external API.

Generated from the same route table the server implements, so the docs
cannot drift from behavior. Covers every endpoint, the error envelope,
auth scheme, rate-limit headers, versioning, and the deprecation policy.
"""
from __future__ import annotations

SCOPES = [
    "sessions:read", "sessions:write",
    "runs:read", "runs:write",
    "approvals:read", "approvals:decide",
    "artifacts:read", "artifacts:write",
    "webhooks:write", "admin",
]

ERROR_CODES = [
    "INVALID_API_KEY", "MISSING_API_KEY", "INSUFFICIENT_SCOPE",
    "VALIDATION_ERROR", "NOT_FOUND", "METHOD_NOT_ALLOWED",
    "RATE_LIMITED", "IDEMPOTENCY_KEY_REUSED", "RUN_ALREADY_TERMINAL",
    "APPROVAL_NOT_PENDING", "APPROVAL_HASH_MISMATCH", "APPROVAL_EXPIRED",
    "INTERNAL_ERROR", "SERVICE_UNAVAILABLE",
]

SSE_EVENTS = [
    "run.status", "assistant.delta", "approval.required",
    "approval.decided", "tool.result", "run.completed",
    "run.failed", "run.cancelled",
]


def build_openapi() -> dict:
    def op(summary: str, scope: str, **kw) -> dict:
        d = {
            "summary": summary,
            "security": [{"bearerAuth": []}],
            "responses": {
                "200": {"description": "OK"},
                "401": {"description": "Missing or invalid API key"},
                "403": {"description": "Key lacks the required scope"},
                "429": {"description": "Rate limited; see Retry-After"},
            },
        }
        d["x-required-scope"] = scope
        d.update(kw)
        return d

    paths = {
        "/v1": {
            "get": {
                "summary": "API version info",
                "security": [],
                "responses": {"200": {"description": "Version descriptor"}},
            }
        },
        "/v1/sessions": {
            "post": op("Create a chat session", "sessions:write", responses={
                "201": {"description": "Session created"},
                **op("", "")["responses"],
            }),
        },
        "/v1/sessions/{session_id}": {
            "get": op("Get a session", "sessions:read"),
        },
        "/v1/sessions/{session_id}/messages": {
            "post": op("Send a message and start a run (idempotent)", "runs:write"),
        },
        "/v1/chats/{chat_id}/messages": {
            "post": op("Send a message and start a run (blueprint-canonical path, idempotent)",
                       "runs:write"),
        },
        "/v1/runs/{run_id}": {
            "get": op("Get run status", "runs:read"),
        },
        "/v1/runs/{run_id}/events": {
            "get": {
                "summary": "SSE stream of run events; reconnect with Last-Event-ID",
                "security": [{"bearerAuth": []}],
                "parameters": [
                    {"name": "last_event_id", "in": "query",
                     "description": "Replay events after this sequence number"},
                ],
                "responses": {"200": {"description": "text/event-stream"}},
                "x-required-scope": "runs:read",
            }
        },
        "/v1/runs/{run_id}/cancel": {
            "post": op("Cancel a run (cooperative for model calls, immediate for queued work)",
                       "runs:write"),
        },
        "/v1/approvals/{approval_id}": {
            "get": op("Get a parked approval request", "approvals:read"),
        },
        "/v1/approvals/{approval_id}/decision": {
            "post": op("Decide a parked approval; argument_hash must match exactly",
                       "approvals:decide"),
        },
        "/v1/artifacts": {
            "post": op("Upload an artifact (base64 JSON)", "artifacts:write"),
        },
        "/v1/artifacts/{artifact_id}": {
            "get": op("Download an artifact", "artifacts:read"),
        },
        "/v1/webhooks": {
            "get": op("List webhook subscriptions", "webhooks:write"),
            "post": op("Subscribe to run completion events", "webhooks:write"),
        },
        "/v1/webhooks/{webhook_id}": {
            "delete": op("Remove a webhook subscription", "webhooks:write"),
        },
        "/openapi.json": {
            "get": {
                "summary": "This OpenAPI document",
                "security": [],
                "responses": {"200": {"description": "OpenAPI 3.0 JSON"}},
            }
        },
    }

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "OpenMuse External API",
            "version": "1.0.0",
            "description": (
                "Versioned public API over the OpenMuse agent platform. "
                "All mutating calls accept Idempotency-Key. Turn progress "
                "streams as server-sent events with Last-Event-ID reconnect. "
                "Errors use a stable machine-readable code envelope."
            ),
        },
        "servers": [{"url": "/"}],
        "security": [{"bearerAuth": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "omk_...",
                    "description": "API key with declared scopes",
                }
            },
            "schemas": {
                "Error": {
                    "type": "object",
                    "properties": {
                        "error": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string", "enum": ERROR_CODES},
                                "message": {"type": "string"},
                                "request_id": {"type": "string"},
                                "details": {"type": "object"},
                            },
                        }
                    },
                },
                "SseEventType": {"type": "string", "enum": SSE_EVENTS},
                "Scope": {"type": "string", "enum": SCOPES},
            },
            "headers": {
                "RateLimit": {
                    "description": "429 responses carry Retry-After (seconds)"
                }
            },
        },
        "x-versioning": {
            "current": "v1",
            "supported": ["v1"],
            "deprecation_policy": (
                "Breaking changes ship as a new /vN prefix. The previous "
                "major version is supported for at least 6 months after the "
                "new version's GA date, announced via the Deprecation and "
                "Sunset response headers and the changelog. Additive changes "
                "(new endpoints, new optional fields, new event types) do not "
                "bump the major version."
            ),
        },
        "x-rate-limiting": (
            "Per API key token bucket. 429 responses include Retry-After. "
            "Clients must back off; aggressive retry loops may have keys throttled."
        ),
    }
