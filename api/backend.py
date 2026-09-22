"""
ApiBackend — the external API's service layer over the agent platform.

Owns the RunStore, tool registry, policy engine, approval service, and the
turn-engine Deps. Mutating calls (send message, approve, cancel) drive real
turns via agent.turn_engine.advance_run in background threads; internal
EventLog entries are translated into the versioned SSE event stream
(run.status, assistant.delta, approval.required, run.completed, ...).

The backend is deliberately thin: authorization policy, idempotency, and
approvals all live in their Phase 1/6 homes. This layer only translates
between the HTTP surface and the platform.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass

from agent import RunStore, ContextBuilder, Deps, advance_run
from agent.models import TERMINAL, WAITING_FOR_APPROVAL
from gateway import Block, ChatMessage, Router
from gateway.providers.mock import ProgrammableMockProvider
from observability import EventLog
from policy import PolicyEngine, ApprovalService, ManualDecider
from tools import ToolRegistry
from tools.namespaces import (
    system_tools, math_tools, file_tools, shell_tools, web_tools, memory_tools,
)

from . import models as M
from .auth import ApiKeyStore
from .eventbus import RunEventBus
from .idempotency import IdempotencyStore
from .ratelimit import RateLimiter
from .webhooks import WebhookRegistry

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    loaded: set[str] = set()
    system_tools.register(registry, loaded_namespaces=loaded)
    math_tools.register(registry)
    file_tools.register(registry)
    shell_tools.register(registry)
    web_tools.register(registry)
    memory_tools.register(registry)
    return registry


@dataclass
class ApiRequestRecord:
    request_id: str
    method: str
    path: str
    status: int
    latency_ms: float
    key_id: str
    occurred_at: float


class ApiBackend:
    """All API state. Thread-safe where runs execute concurrently."""

    def __init__(
        self,
        *,
        workspace_root: str,
        respond=None,
        webhook_deliver=None,
        tenant_id: str = "tenant_demo",
        default_namespaces: set[str] | None = None,
    ):
        self.tenant_id = tenant_id
        self.workspace_root = workspace_root
        # Server policy: namespaces offered to the model on every API run.
        # Deferred discovery (tools.load_namespace) still works for the rest;
        # it mutates the registry's set, which the shared server registry
        # tolerates because the context builder reads run.loaded_namespaces.
        self.default_namespaces = default_namespaces or {"system", "files"}
        self.keys = ApiKeyStore()
        self.runs = RunStore()
        self.registry = build_registry()
        self.policy = PolicyEngine(os.path.join(ROOT, "policies", "tool-capabilities.yaml"))
        self.approvals = ApprovalService()
        self.decider = ManualDecider()  # production path: approvals resolve via the API
        self.context_builder = ContextBuilder(
            prompts_dir=os.path.join(ROOT, "prompts"),
            registry=self.registry,
            agent_name="openmuse",
            channel="api",
        )
        provider = ProgrammableMockProvider(respond or self._default_respond)
        self.gateway = Router({"planner": [provider], "fast": [provider]})

        self.sessions: dict[str, M.SessionRecord] = {}
        self.eventbus = RunEventBus()
        self.idempotency = IdempotencyStore()
        self.rate_limiter = RateLimiter()
        self.webhooks = WebhookRegistry(deliver=webhook_deliver) if webhook_deliver else WebhookRegistry()
        self.artifacts: dict[str, M.ArtifactRecord] = {}
        self._artifact_bytes: dict[str, bytes] = {}

        self._logs: dict[str, EventLog] = {}        # run_id -> internal event log
        self._emitted: dict[str, int] = {}          # run_id -> internal events translated
        self._run_locks: dict[str, threading.Lock] = {}
        self._pending_approval: dict[str, str] = {}  # run_id -> latest parked approval_id
        self.request_log: list[ApiRequestRecord] = []  # immutable per-request audit
        self._req_lock = threading.Lock()

    # -- platform deps ------------------------------------------------------
    def _deps(self) -> Deps:
        return Deps(
            gateway=self.gateway,
            registry=self.registry,
            policy=self.policy,
            approvals=self.approvals,
            decider=self.decider,
            context_builder=self.context_builder,
            workspace_root=self.workspace_root,
        )

    def _run_lock(self, run_id: str) -> threading.Lock:
        return self._run_locks.setdefault(run_id, threading.Lock())

    @staticmethod
    def _default_respond(request, history):
        from gateway import ModelResponse
        return ModelResponse(text="ok", stop_reason="stop")

    # -- request audit ------------------------------------------------------
    def log_request(self, *, request_id: str, method: str, path: str,
                    status: int, latency_ms: float, key_id: str) -> None:
        with self._req_lock:
            self.request_log.append(ApiRequestRecord(
                request_id=request_id, method=method, path=path, status=status,
                latency_ms=latency_ms, key_id=key_id, occurred_at=time.time(),
            ))

    # -- sessions -----------------------------------------------------------
    def create_session(self, *, user_id: str, title: str = "") -> M.SessionRecord:
        sid = M._new("sess")
        rec = M.SessionRecord(session_id=sid, chat_id=sid, tenant_id=self.tenant_id,
                              user_id=user_id, title=title)
        self.sessions[sid] = rec
        return rec

    # -- messages / runs ----------------------------------------------------
    @staticmethod
    def _blocks_from_content(content: list) -> list[Block]:
        blocks = []
        for item in content or []:
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                blocks.append(Block(kind="text", text=item["text"], trust="user"))
        if not blocks:
            raise ValueError("content must contain at least one text block")
        return blocks

    def submit_message(self, *, chat_id: str, user_id: str, content: list,
                       idempotency_key: str = "") -> tuple:
        """Returns (run, created). Starts background execution for new runs."""
        msg = ChatMessage(role="user", blocks=self._blocks_from_content(content),
                          tool_calls=[])
        msg_id = "msg_" + uuid.uuid4().hex[:12]
        run, created = self.runs.submit(
            tenant_id=self.tenant_id, chat_id=chat_id, user_id=user_id,
            user_message=msg, idempotency_key=idempotency_key or "",
        )
        if created:
            run.loaded_namespaces.update(self.default_namespaces)
            self._logs[run.run_id] = EventLog(run.run_id)
            self._emitted[run.run_id] = 0
            self.eventbus.publish(run.run_id, M.SSE_RUN_STATUS,
                                  {"status": run.state, "message_id": msg_id})
            t = threading.Thread(target=self._execute_run, args=(run.run_id,),
                                 daemon=True, name=f"run-{run.run_id}")
            t.start()
        return run, created, msg_id

    def _execute_run(self, run_id: str) -> None:
        with self._run_lock(run_id):
            run = self.runs.get(run_id)
            advance_run(run, self._deps(), self._logs[run_id])
            self._translate_new_events(run_id)
            if run.state == WAITING_FOR_APPROVAL:
                return  # parked; the decision endpoint resumes via resume_run()
            self._finish_run(run_id)

    def resume_run(self, run_id: str) -> None:
        t = threading.Thread(target=self._execute_run, args=(run_id,),
                             daemon=True, name=f"run-{run_id}-resume")
        t.start()

    def cancel_run(self, run_id: str) -> dict:
        run = self.runs.get(run_id)
        if run.state in TERMINAL:
            raise _Terminal("run already terminal")
        if run.state == WAITING_FOR_APPROVAL:
            # Parked runs are queued work: cancellation is immediate.
            run.transition("CANCELLED")
            self._logs[run_id].append("run.cancelled", {"via": "api"})
            self._translate_new_events(run_id)
            self._finish_run(run_id)
        else:
            run.cancel_requested = True  # cooperative for in-flight model calls
        return {"run_id": run_id, "status": run.state}

    # -- internal event -> SSE translation ----------------------------------
    def _translate_new_events(self, run_id: str) -> None:
        run = self.runs.get(run_id)
        log = self._logs[run_id]
        start = self._emitted.get(run_id, 0)
        last_requested: dict = {}
        for evt in log.events[start:]:
            p = evt.payload or {}
            t = evt.type
            if t == "run.transition":
                self.eventbus.publish(run_id, M.SSE_RUN_STATUS, {"status": p.get("to")})
            elif t == "approval.requested":
                last_requested = p
            elif t == "approval.parked":
                aid = p.get("approval_id") or last_requested.get("approval_id", "")
                self._pending_approval[run_id] = aid
                self.eventbus.publish(run_id, M.SSE_APPROVAL_REQUIRED, {
                    "approval_id": aid,
                    "tool": last_requested.get("tool", ""),
                    "risk": last_requested.get("risk", ""),
                    "presentation": last_requested.get("bind_fields", {}),
                })
            elif t == "approval.decided":
                self.eventbus.publish(run_id, M.SSE_APPROVAL_DECIDED, {
                    "approval_id": p.get("approval_id"), "verdict": p.get("verdict"),
                })
            elif t == "tool.rejected":
                self.eventbus.publish(run_id, M.SSE_TOOL_RESULT, {
                    "call_id": p.get("call_id"), "tool": p.get("tool"),
                    "ok": False, "code": p.get("code"),
                })
            elif t == "tool.executed":
                self.eventbus.publish(run_id, M.SSE_TOOL_RESULT, {
                    "call_id": p.get("call_id"), "tool": p.get("tool"),
                    "ok": p.get("ok", True),
                })
            elif t == "run.completed":
                if run.final_text:
                    self.eventbus.publish(run_id, M.SSE_ASSISTANT_DELTA,
                                          {"text": run.final_text})
                self.eventbus.publish(run_id, M.SSE_RUN_COMPLETED, {
                    "run_id": run_id,
                    "message_id": "msg_assistant_" + run_id.split("_", 1)[1],
                    "final_chars": p.get("final_chars", 0),
                })
            elif t == "run.failed":
                self.eventbus.publish(run_id, M.SSE_RUN_FAILED, {
                    "run_id": run_id, "code": p.get("code"),
                    "message": p.get("message", ""),
                })
            elif t == "run.cancelled":
                self.eventbus.publish(run_id, M.SSE_RUN_CANCELLED, {"run_id": run_id})
        self._emitted[run_id] = len(log.events)

    def _finish_run(self, run_id: str) -> None:
        run = self.runs.get(run_id)
        event = "run.completed" if run.state == "COMPLETED" else "run.failed"
        if run.state == "CANCELLED":
            event = "run.failed"
        self.webhooks.notify(
            tenant_id=self.tenant_id, event=event,
            payload={"run_id": run_id, "chat_id": run.chat_id, "status": run.state,
                     "final_chars": len(run.final_text or "")},
        )

    # -- approvals ----------------------------------------------------------
    def decide_approval(self, approval_id: str, *, decision: str,
                        argument_hash: str, decided_by: str) -> dict:
        req = self.approvals.requests.get(approval_id)
        if req is None:
            raise _NotFound("approval")
        if req.status != "pending":
            raise _NotPending(f"approval {approval_id} already {req.status}")
        if time.time() > req.created_at + req.expires_in_seconds:
            req.status = "expired"
            raise _Expired("approval expired")
        # The client must echo the exact bound argument hash; modified
        # arguments can never ride through this endpoint (blueprint contract).
        if argument_hash != req.argument_hash:
            raise _HashMismatch("argument_hash does not match the parked request")
        # External vocabulary ("approve"/"deny", per the API contract) maps
        # onto the internal ApprovalService vocabulary ("approved"/"denied").
        internal = {"approve": "approved", "deny": "denied",
                    "approved": "approved", "denied": "denied"}[decision]
        grant = self.approvals.resolve(approval_id, internal, decided_by=decided_by)
        self._logs[req.run_id].append("approval.decided",
                                      {"approval_id": approval_id, "verdict": decision,
                                       "via": "api"})
        self._translate_new_events(req.run_id)
        self.resume_run(req.run_id)
        return {
            "approval_id": approval_id,
            "status": req.status,
            "grant_id": grant.id if grant else None,
            "run_id": req.run_id,
        }

    # -- artifacts ----------------------------------------------------------
    def store_artifact(self, *, name: str, content_b64: str, content_type: str) -> M.ArtifactRecord:
        import base64
        raw = base64.b64decode(content_b64.encode("ascii"))
        rec = M.ArtifactRecord(
            artifact_id=M._new("art"), tenant_id=self.tenant_id, name=name,
            content_type=content_type or "application/octet-stream",
            size=len(raw),
            sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
        )
        self.artifacts[rec.artifact_id] = rec
        self._artifact_bytes[rec.artifact_id] = raw
        return rec

    def get_artifact(self, artifact_id: str) -> tuple[M.ArtifactRecord, bytes]:
        rec = self.artifacts.get(artifact_id)
        if rec is None or rec.tenant_id != self.tenant_id:
            raise _NotFound("artifact")
        return rec, self._artifact_bytes[artifact_id]


class _NotFound(Exception):
    pass


class _NotPending(Exception):
    pass


class _Expired(Exception):
    pass


class _HashMismatch(Exception):
    pass


class _Terminal(Exception):
    pass
