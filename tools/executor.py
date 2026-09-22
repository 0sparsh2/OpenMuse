"""
Tool-call lifecycle executor.

Per blueprint "Tool-call lifecycle":
  1. Parse provider output; reject unknown tool names and invalid JSON.
  2. Validate arguments against the exact registered schema.
  3. Canonicalize arguments and compute argument_hash.
  4. Classify data flow; ask policy for ALLOW / ASK / DENY.
  5. Execute authorized calls with deadline and resource limits.
  6. Validate output, redact protected values, return a bounded model view.
  7. Append audit events.

Steps 1-3 live in `prevalidate` (unknown tools and invalid arguments never
reach executors). Step 4 is done by the turn engine via the policy engine.
This module handles 5-7 plus envelope construction.

Tool errors are returned as data with recovery hints, never raw tracebacks.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from jsonschema import ValidationError, validate

from observability.events import EventLog
from tools.redaction import redact_text
from tools.registry import ToolDefinition, ToolRegistry, UnknownToolError

MODEL_VIEW_CHAR_LIMIT = 8000


# ---------------------------------------------------------------------------
# Prevalidation
# ---------------------------------------------------------------------------
@dataclass
class PrevalidatedCall:
    call_id: str
    tool_name: str
    arguments: dict
    argument_hash: str
    tool: Optional[ToolDefinition] = None
    # set when the call is rejected before execution
    error: Optional[dict] = None


def canonicalize(value: Any) -> Any:
    """NFKC-normalize strings, sort object keys, drop explicit nulls."""
    if isinstance(value, str):
        return unicodedata.normalize("NFKC", value)
    if isinstance(value, dict):
        return {k: canonicalize(value[k]) for k in sorted(value) if value[k] is not None}
    if isinstance(value, list):
        return [canonicalize(v) for v in value]
    return value


def argument_hash(tool_name: str, version: str, canonical_args: dict) -> str:
    payload = f"{tool_name}||{version}||{json.dumps(canonical_args, sort_keys=True, separators=(',', ':'))}"
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def prevalidate(registry: ToolRegistry, call_id: str, tool_name: str, arguments: Any) -> PrevalidatedCall:
    """Parse + schema-validate. Never raises for model mistakes."""
    try:
        tool = registry.get(tool_name)
    except UnknownToolError:
        return PrevalidatedCall(
            call_id=call_id,
            tool_name=tool_name,
            arguments={},
            argument_hash="",
            error={
                "code": "UNKNOWN_TOOL",
                "retryable": False,
                "safe_message": (
                    f"There is no tool named {tool_name!r}. "
                    f"Known tools: {', '.join(registry.tool_names()) or '(none)'}. "
                    "Use tools.load_namespace to discover tools."
                ),
            },
        )
    if not isinstance(arguments, dict):
        return PrevalidatedCall(
            call_id=call_id, tool_name=tool_name, arguments={}, argument_hash="",
            error={"code": "INVALID_ARGUMENTS", "retryable": False,
                   "safe_message": f"Arguments for {tool_name} must be a JSON object."},
        )
    try:
        validate(instance=arguments, schema=tool.input_schema)
    except ValidationError as exc:
        return PrevalidatedCall(
            call_id=call_id, tool_name=tool_name, arguments={}, argument_hash="",
            error={"code": "INVALID_ARGUMENTS", "retryable": False,
                   "safe_message": f"Invalid arguments for {tool_name}: {exc.message}"},
        )
    canonical = canonicalize(arguments)
    return PrevalidatedCall(
        call_id=call_id,
        tool_name=tool_name,
        arguments=canonical,
        argument_hash=argument_hash(tool_name, tool.version, canonical),
        tool=tool,
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
@dataclass
class ExecutionContext:
    run_id: str
    tenant_id: str
    workspace_root: str          # all scoped file/shell tools are jailed here
    event_log: EventLog
    approval_grant_id: str = ""  # set when this call runs under an ASK grant
    deadline_ms: int = 30_000


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_model_view(output: Any) -> tuple[Any, bool]:
    """Truncate the model view; return (view, truncated)."""
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    if len(text) > MODEL_VIEW_CHAR_LIMIT:
        view = text[:MODEL_VIEW_CHAR_LIMIT] + f"\n…[truncated {len(text) - MODEL_VIEW_CHAR_LIMIT} chars]"
        return (view if isinstance(output, str) else {"truncated_text": view}), True
    return output, False


def _redact_view(view: Any) -> tuple[Any, list[str]]:
    text = view if isinstance(view, str) else json.dumps(view, ensure_ascii=False)
    redacted, labels = redact_text(text)
    if not labels:
        return view, []
    if isinstance(view, str):
        return redacted, labels
    return {"redacted_text": redacted}, labels


def _failed_envelope(call: PrevalidatedCall, code: str, safe_message: str, *, retryable: bool = False) -> dict:
    return {
        "call_id": call.call_id,
        "tool": call.tool_name,
        "tool_version": call.tool.version if call.tool else "unknown",
        "status": "failed",
        "started_at": _utcnow(),
        "finished_at": _utcnow(),
        "model_view": None,
        "full_result_ref": None,
        "truncated": False,
        "redactions": [],
        "postconditions": [],
        "error": {"code": code, "retryable": retryable, "safe_message": safe_message},
        "metrics": {"latency_ms": 0},
    }


def _execute_one(ctx: ExecutionContext, call: PrevalidatedCall) -> dict:
    assert call.tool is not None and call.tool.execute is not None
    started = _utcnow()
    t0 = time.monotonic()

    def _run():
        return call.tool.execute(ctx, call.arguments)

    timeout_s = min(ctx.deadline_ms, call.tool.default_timeout_ms) / 1000.0
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_run)
        try:
            output = future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return _failed_envelope(call, "TIMEOUT", f"{call.tool_name} exceeded its deadline.", retryable=call.tool.idempotency == "pure")
        except Exception as exc:  # tool bugs become data, never tracebacks
            return _failed_envelope(
                call, "TOOL_EXECUTION_FAILED",
                f"{call.tool_name} failed: {type(exc).__name__}",
                retryable=call.tool.idempotency == "pure",
            )

    # validate output schema (best effort — a bad tool is a failed call)
    try:
        validate(instance=output, schema=call.tool.output_schema)
    except ValidationError as exc:
        return _failed_envelope(call, "INVALID_TOOL_OUTPUT", f"{call.tool_name} returned malformed output: {exc.message}")

    view, truncated = _bounded_model_view(output)
    view, redactions = _redact_view(view)
    latency_ms = int((time.monotonic() - t0) * 1000)

    envelope = {
        "call_id": call.call_id,
        "tool": call.tool_name,
        "tool_version": call.tool.version,
        "status": "succeeded",
        "started_at": started,
        "finished_at": _utcnow(),
        "model_view": view,
        "full_result_ref": f"obj://tool-results/{ctx.run_id}/{call.call_id}",
        "truncated": truncated,
        "redactions": redactions,
        "postconditions": [f"side_effect:{call.tool.side_effect}"],
        "metrics": {"latency_ms": latency_ms},
    }
    ctx.event_log.append(
        "tool.result",
        {"call_id": call.call_id, "tool": call.tool_name, "status": "succeeded",
         "latency_ms": latency_ms, "redactions": redactions,
         "grant_id": ctx.approval_grant_id or None},
    )
    return envelope


def execute_batch(
    registry: ToolRegistry,
    ctx: ExecutionContext,
    calls: list[PrevalidatedCall],
) -> list[dict]:
    """
    Execute authorized calls. Pure reads run in parallel; anything with a
    side effect is serialized in call order (conflict-key partitioning is a
    later-phase concern; Phase 1 serializes all writes).
    """
    envelopes: list[dict] = []
    pure = [c for c in calls if c.tool and c.tool.side_effect == "none"]
    effectful = [c for c in calls if c.tool and c.tool.side_effect != "none"]

    if pure:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(pure))) as pool:
            for envelope in pool.map(lambda c: _execute_one(ctx, c), pure):
                envelopes.append(envelope)
    for call in effectful:
        envelopes.append(_execute_one(ctx, call))

    # keep response order aligned with request order
    order = {c.call_id: i for i, c in enumerate(calls)}
    envelopes.sort(key=lambda e: order[e["call_id"]])
    return envelopes


def rejection_envelope(call: PrevalidatedCall) -> dict:
    """Envelope for calls rejected in prevalidation (never reached an executor)."""
    assert call.error is not None
    return _failed_envelope(call, call.error["code"], call.error["safe_message"], retryable=call.error.get("retryable", False))
