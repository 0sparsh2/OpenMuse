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
import re
import threading
import time
import uuid
from dataclasses import dataclass

from agent import RunStore, ContextBuilder, Deps, advance_run
from agent.models import TERMINAL, WAITING_FOR_APPROVAL, RunBudgets
from gateway import Block, ChatMessage, Router
from gateway.providers.mock import ProgrammableMockProvider
from observability import EventLog
from policy import PolicyEngine, ApprovalService, ManualDecider
from tools import ToolRegistry
from tools.namespaces import (
    system_tools, math_tools, file_tools, shell_tools, web_tools, memory_tools, task_tools,
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
    task_tools.register(registry)
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
        browser_operator=None,
        run_budgets: RunBudgets | None = None,
        memory_service=None,
        accounts_root: str | None = None,
        db_path: str | None = None,
        scheduling_root: str | None = None,
        connectors=None,
        monitors_llm=None,
        enable_monitors: bool = False,
    ):
        self.tenant_id = tenant_id
        self.workspace_root = workspace_root
        # Server policy: namespaces offered to the model on every API run.
        # Deferred discovery (tools.load_namespace) still works for the rest;
        # it mutates the registry's set, which the shared server registry
        # tolerates because the context builder reads run.loaded_namespaces.
        self.default_namespaces = default_namespaces or {"system", "files", "task"}
        self.keys = ApiKeyStore()
        self.runs = RunStore()
        self.registry = build_registry()
        # The interactive API asks the user inline for external writes (R3).
        self.policy = PolicyEngine(os.path.join(ROOT, "policies", "tool-capabilities.yaml"),
                                   ask_for_external_writes=True)
        self.approvals = ApprovalService()
        self.run_budgets = run_budgets
        # Optional live browser (browser.live_operator): registers browser.* and
        # powers the /v1/browser/sessions live-view endpoints.
        self.browser = browser_operator
        if browser_operator is not None:
            from browser import register as register_browser
            register_browser(self.registry, browser_operator, approvals=self.approvals)
        self._browser_runs: dict[str, str] = {}   # browser session_id -> latest run_id
        self._chat_browser: dict[str, str] = {}   # chat_id -> open browser session_id
        self._translate_locks: dict[str, threading.RLock] = {}
        self._run_text: dict[str, str] = {}          # run_id -> the user's message text
        self._plans: dict[str, dict] = {}             # run_id -> {"goal", "steps": [...]}
        self._receipts: dict[str, dict] = {}
        # Per-user memory (memory.service.MemoryService): each user's runs get
        # their own workspace + memory root, profile/MEMORY.md/recall context,
        # background extraction, and summary-based chat history.
        self.memory = memory_service
        if memory_service is not None:
            from tools.namespaces import memory_tools as _mt
            _mt.SERVICE = memory_service
            self.default_namespaces = set(self.default_namespaces) | {"memory"}
        self.accounts = None
        if accounts_root:
            from .accounts import AccountStore
            self.accounts = AccountStore(accounts_root, self.keys, self.tenant_id)
        self._last_requested: dict[str, dict] = {}
        self.decider = ManualDecider()  # production path: approvals resolve via the API
        self.context_builder = ContextBuilder(
            prompts_dir=os.path.join(ROOT, "prompts"),
            registry=self.registry,
            agent_name="openmuse",
            channel="api",
        )
        if memory_service is not None:
            self.context_builder.memory_provider = self._memory_for_run
            self.context_builder.timezone_provider = lambda run: self.user_timezone(run.user_id)
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

        # Notifications (issue #5): durable per-user inbox + live listeners.
        self._notes_mem: list[dict] = []           # used when there is no database
        self._note_listeners: list = []            # callables(user_id, note)
        self._watchers: dict[str, int] = {}        # run_id -> open SSE streams

        # Durable persistence (issue #2): chats, runs, SSE events, approvals.
        self.db = None
        if db_path:
            from storage import Store
            self.db = Store(db_path)
            self._restore()
            _publish = self.eventbus.publish

            def publish(run_id, type, data, _publish=_publish):
                evt = _publish(run_id, type, data)
                self.db.append_event(run_id, evt.seq, evt.type, evt.data, evt.occurred_at)
                return evt
            self.eventbus.publish = publish

        # App connectors via Composio (issue #7): per-user connected accounts.
        self.apps = connectors
        if connectors is not None:
            bridged = connectors.register_tools(self.registry)
            print(f"connectors: {len(bridged)} tools bridged", flush=True)

        # Monitors & alerts (issue #11): price / text / change watches.
        self.monitors = None
        if enable_monitors:
            from .monitors import Monitors, register_tools as register_monitor_tools
            self.monitors = Monitors(self, llm=monitors_llm)
            register_monitor_tools(self.registry, self.monitors)

        # Per-user schedules (issue #4): tools + runner thread (started by the host).
        self.schedules = None
        if scheduling_root:
            from scheduler import namespace as scheduler_ns
            from .scheduling import UserSchedules
            self.schedules = UserSchedules(self, scheduling_root)
            scheduler_ns.register(
                self.registry, lambda ctx: self.schedules.service_for(getattr(ctx, "user_id", "")),
                default_timezone=lambda ctx: self.user_timezone(getattr(ctx, "user_id", "")))

    # -- per-user profile helpers -------------------------------------------------
    def user_timezone(self, user_id: str) -> str:
        """The user's IANA timezone from USER.md ("- Timezone: America/Chicago")."""
        if self.memory is not None and user_id:
            try:
                text = self.memory.read_profile(user_id).get("USER.md", "")
                m = re.search(r"^\s*-?\s*Timezone:\s*([A-Za-z_]+/[A-Za-z_/+-]+|UTC)\s*$", text, re.M | re.I)
                if m:
                    from zoneinfo import ZoneInfo
                    ZoneInfo(m.group(1))
                    return m.group(1)
            except Exception:
                pass
        return "UTC"

    # -- notifications (issue #5) ------------------------------------------------------
    def notify(self, user_id: str, *, kind: str, title: str, body: str = "",
               link: dict | None = None, dedupe_key: str = "") -> dict | None:
        now = time.time()
        existing = self.notifications(user_id, limit=200)
        if dedupe_key and any(n.get("dedupe_key") == dedupe_key and now - n["created_at"] < 86400
                              for n in existing):
            return None
        note = {"id": "ntf_" + uuid.uuid4().hex[:12], "user_id": user_id, "kind": kind,
                "title": title[:160], "body": body[:600], "link": link or {},
                "dedupe_key": dedupe_key, "created_at": now, "read_at": None}
        if self.db is not None:
            self.db.kv_put("notifications", note["id"], note, user_id=user_id)
        else:
            self._notes_mem.append(note)
        for fn in list(self._note_listeners):
            try:
                fn(user_id, note)
            except Exception:
                pass
        return note

    def notifications(self, user_id: str, *, unread_only: bool = False, limit: int = 100) -> list[dict]:
        if self.db is not None:
            items = self.db.kv_list("notifications", user_id=user_id, limit=limit)
        else:
            items = [n for n in reversed(self._notes_mem) if n["user_id"] == user_id][:limit]
        items.sort(key=lambda n: n["created_at"], reverse=True)
        return [n for n in items if not (unread_only and n.get("read_at"))]

    def mark_read(self, user_id: str, note_id: str | None = None) -> int:
        n = 0
        for note in self.notifications(user_id, unread_only=True, limit=500):
            if note_id and note["id"] != note_id:
                continue
            note["read_at"] = time.time()
            if self.db is not None:
                self.db.kv_put("notifications", note["id"], note, user_id=user_id)
            n += 1
        return n

    def _notify_run_end(self, run) -> None:
        if self.schedules is not None:
            self.schedules.on_run_finished(run)
        if self._watchers.get(run.run_id, 0) > 0:
            return  # the user watched it happen; no need to ping
        sched_chat = run.chat_id and self.sessions.get(run.chat_id) and self.sessions[run.chat_id].title == "Scheduled"
        if sched_chat:
            return  # schedule results notify via the scheduler
        ok = run.state == "COMPLETED"
        title = self.sessions[run.chat_id].title if run.chat_id in self.sessions else "Task"
        self.notify(run.user_id, kind="run", title=("Finished: " if ok else "Didn't finish: ") + (title or "task"),
                    body=(run.final_text or run.failure_message or "")[:280],
                    link={"chat_id": run.chat_id, "run_id": run.run_id}, dedupe_key=f"run:{run.run_id}")

    # -- persistence ------------------------------------------------------------
    def _save_run(self, run) -> None:
        if self.db is not None:
            self.db.save_run(run, self._run_text.get(run.run_id, ""))
            for req in list(self.approvals.requests.values()):
                if req.run_id == run.run_id:
                    self.db.save_approval("request", req)
            for g in list(self.approvals.grants.values()):
                if g.run_id == run.run_id:
                    self.db.save_approval("grant", g)

    def _restore(self) -> None:
        """Reload durable state. Runs that were mid-flight when the process
        died are failed (WORKER_RESTARTED), never replayed; runs parked on an
        approval stay parked and resume normally once approved."""
        from policy.approvals import ApprovalGrant, ApprovalRequest
        from storage.db import run_from_dict
        from .eventbus import SseEvent
        for c in self.db.chats():
            self.sessions[c["chat_id"]] = M.SessionRecord(
                session_id=c["chat_id"], chat_id=c["chat_id"], tenant_id=c["tenant_id"],
                user_id=c["user_id"], title=c["title"], created_at=c["created_at"])
        for row in self.db.events():
            self.eventbus._events.setdefault(row["run_id"], []).append(SseEvent(
                seq=row["seq"], type=row["type"], data=json.loads(row["data"]),
                occurred_at=row["occurred_at"]))
        for kind, d in self.db.approvals():
            if kind == "request":
                req = ApprovalRequest(**d)
                self.approvals.requests[req.id] = req
            else:
                g = ApprovalGrant(**d)
                self.approvals.grants[g.id] = g
        interrupted = []
        for snap, user_text in self.db.runs():
            run = run_from_dict(snap)
            self.runs._runs[run.run_id] = run
            if run.idempotency_key:
                self.runs._by_idempotency[run.idempotency_key] = run.run_id
            self._run_text[run.run_id] = user_text
            self._logs[run.run_id] = EventLog(run.run_id)
            self._emitted[run.run_id] = 0
            if run.state == "PAUSED":
                pass  # stays paused; the user resumes it
            elif run.state == WAITING_FOR_APPROVAL:
                for req in self.approvals.requests.values():
                    if req.run_id == run.run_id and req.status == "pending":
                        self._pending_approval[run.run_id] = req.id
            elif run.state not in TERMINAL:
                interrupted.append(run)
        for run in interrupted:
            run.state = "FAILED"
            run.failure_code = "WORKER_RESTARTED"
            run.failure_message = ("The server restarted while this task was running. "
                                   "Nothing was retried automatically — ask again to continue.")
            self.eventbus.publish(run.run_id, M.SSE_RUN_FAILED, {
                "run_id": run.run_id, "code": run.failure_code, "message": run.failure_message})
            self.eventbus.publish(run.run_id, M.SSE_RUN_STATUS, {"status": "FAILED"})
            for seq_evt in self.eventbus._events.get(run.run_id, [])[-2:]:
                self.db.append_event(run.run_id, seq_evt.seq, seq_evt.type, seq_evt.data,
                                     seq_evt.occurred_at)
            self.db.save_run(run)

    # -- platform deps ------------------------------------------------------
    def _deps(self, run=None) -> Deps:
        workspace, memory_root = self.workspace_root, None
        if self.memory is not None and run is not None:
            workspace = self.memory.workspace_root(run.user_id)
            memory_root = self.memory.memory_root(run.user_id)
        return Deps(
            gateway=self.gateway,
            registry=self.registry,
            policy=self.policy,
            approvals=self.approvals,
            decider=self.decider,
            context_builder=self.context_builder,
            workspace_root=workspace,
            memory_root=memory_root,
            user_id=run.user_id if run is not None else "",
        )

    def _apps_note(self, run) -> list:
        if self.apps is None:
            return []
        try:
            conns = self.apps.connections(run.user_id)
        except Exception:
            return []
        from connectors.composio_bridge import TOOLKITS
        parts = [f"{v['name']} ({'connected — tools: ' + v['namespace'] + '.*' if k in conns else 'not connected; the user can connect it in Apps'})"
                 for k, v in TOOLKITS.items()]
        return [ChatMessage(role="user", blocks=[Block(
            kind="text", trust="user",
            text="[Runtime note — trusted] Apps: " + "; ".join(parts) + ". Reading is automatic; "
                 "sending email or changing the calendar always asks the user first.")])]

    def _memory_for_run(self, run):
        if self.memory is None:
            return self._apps_note(run), None
        try:
            blocks, wm = self.memory.turn_context(run.user_id, run.run_id,
                                                  self._run_text.get(run.run_id, ""))
            return list(blocks) + self._apps_note(run), wm
        except Exception as exc:  # memory must never break a turn
            print(f"memory context failed: {exc}", flush=True)
            return [], None

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
        if self.db is not None:
            self.db.save_chat(rec)
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
        history = self._chat_history(chat_id, user_id=user_id)
        run, created = self.runs.submit(
            tenant_id=self.tenant_id, chat_id=chat_id, user_id=user_id,
            user_message=msg, idempotency_key=idempotency_key or "",
            budgets=self.run_budgets,
        )
        if created:
            self._run_text[run.run_id] = " ".join(
                b.text for b in msg.blocks if b.kind == "text")[:4000]
            session = self.sessions.get(chat_id)
            if session is not None and not session.title:
                t = self._run_text[run.run_id].strip().replace("\n", " ")
                session.title = t if len(t) <= 60 else t[:58].rstrip() + "…"
            if self.db is not None and session is not None:
                self.db.update_chat(chat_id, title=session.title)
            run.messages[:0] = history
            self._save_run(run)
            run.loaded_namespaces.update(self.default_namespaces)
            if self._chat_browser.get(chat_id):
                run.loaded_namespaces.add("browser")
            if self.apps is not None:
                try:  # connected apps' tools are ready without a load step
                    run.loaded_namespaces |= self.apps.connected_namespaces(user_id)
                except Exception:
                    pass
            self._logs[run.run_id] = EventLog(run.run_id)
            self._logs[run.run_id].on_append = (
                lambda _evt, rid=run.run_id: self._translate_new_events(rid))
            self._emitted[run.run_id] = 0
            self.eventbus.publish(run.run_id, M.SSE_RUN_STATUS,
                                  {"status": run.state, "message_id": msg_id})
            t = threading.Thread(target=self._execute_run, args=(run.run_id,),
                                 daemon=True, name=f"run-{run.run_id}")
            t.start()
        return run, created, msg_id

    def _turns(self, chat_id: str) -> list[dict]:
        """Finished turns of a chat, oldest first: {seq, run_id, user, assistant}."""
        prior = [r for r in self.runs._runs.values()
                 if r.chat_id == chat_id and r.state in TERMINAL]
        prior.sort(key=lambda r: r.created_at)
        return [{"seq": i + 1, "run_id": r.run_id,
                 "user": self._run_text.get(r.run_id, ""), "assistant": r.final_text or ""}
                for i, r in enumerate(prior) if self._run_text.get(r.run_id)]

    def _chat_history(self, chat_id: str, *, user_id: str = "",
                      max_turns: int = 8) -> list[ChatMessage]:
        """Prior finished turns of this chat, so a follow-up like "book the
        7:30 one" has context. With memory enabled, older turns come from the
        chat's rolling summary (Layer 6) and the latest ones stay verbatim.
        Tool traffic is not replayed; an open browser session is surfaced as a
        runtime note."""
        prior = [r for r in self.runs._runs.values()
                 if r.chat_id == chat_id and r.state in TERMINAL]
        prior.sort(key=lambda r: r.created_at if hasattr(r, "created_at") else 0)
        out: list[ChatMessage] = []
        if self.memory is not None:
            out = self.memory.chat_history(user_id, chat_id, self._turns(chat_id))
            prior = []
        for r in prior[-max_turns:]:
            user = next((m for m in r.messages if m.role == "user"
                         and any(b.trust == "user" for b in m.blocks)), None)
            if user is None:
                continue
            out.append(ChatMessage(role="user", blocks=[b for b in user.blocks
                                                       if b.trust == "user"]))
            if r.final_text:
                out.append(ChatMessage(role="assistant",
                                       blocks=[Block(kind="text", text=r.final_text)]))
        sid = self._chat_browser.get(chat_id)
        info = self.browser.session_info(sid) if (sid and self.browser is not None
                                                 and hasattr(self.browser, "session_info")) else None
        if info and info.get("state") != "closed":
            out.append(ChatMessage(role="user", blocks=[Block(
                kind="text", trust="user",
                text=f"[Runtime note — trusted] Your browser session {sid} is still open "
                     f"at {info.get('url')} ({info.get('title')}). Reuse it with "
                     f"browser.observe / browser.act instead of starting a new one.")]))
        return out

    def _execute_run(self, run_id: str) -> None:
        with self._run_lock(run_id):
            run = self.runs.get(run_id)
            advance_run(run, self._deps(run), self._logs[run_id])
            self._translate_new_events(run_id)
            self._save_run(run)
            if run.state in (WAITING_FOR_APPROVAL, "PAUSED"):
                return  # parked; approval / resume endpoints continue it
            self._finish_run(run_id)
            self._record_receipt(run)
            self._notify_run_end(run)
            if self.memory is not None and run.state == "COMPLETED":
                # asynchronous by design: learning never slows the turn
                noted = any(e.type == "tool.result" and e.payload.get("tool") == "memory.note"
                            and e.payload.get("status") == "succeeded"
                            for e in self._logs[run_id].events)
                self.memory.after_turn(run.user_id, run.chat_id, run_id,
                                       self._run_text.get(run_id, ""), run.final_text or "",
                                       self._turns(run.chat_id), already_noted=noted)

    def resume_run(self, run_id: str) -> None:
        t = threading.Thread(target=self._execute_run, args=(run_id,),
                             daemon=True, name=f"run-{run_id}-resume")
        t.start()

    def cancel_run(self, run_id: str) -> dict:
        run = self.runs.get(run_id)
        if run.state in TERMINAL:
            raise _Terminal("run already terminal")
        if run.state in (WAITING_FOR_APPROVAL, "PAUSED"):
            # Parked/paused runs are queued work: cancellation is immediate.
            run.transition("CANCELLED")
            self._logs[run_id].append("run.cancelled", {"via": "api"})
            self._translate_new_events(run_id)
            self._finish_run(run_id)
            self._save_run(run)
        else:
            run.cancel_requested = True  # cooperative for in-flight model calls
        return {"run_id": run_id, "status": run.state}

    # -- pause / resume / retry (issue #6) -------------------------------------
    def pause_run(self, run_id: str) -> dict:
        run = self.runs.get(run_id)
        if run.state in TERMINAL:
            raise _Terminal("run already terminal")
        run.pause_requested = True       # honoured at the next step boundary
        return {"run_id": run_id, "status": run.state, "pause_requested": True}

    def resume_paused(self, run_id: str) -> dict:
        run = self.runs.get(run_id)
        run.pause_requested = False
        if run.state == "PAUSED":
            self.resume_run(run_id)
        return {"run_id": run_id, "status": run.state}

    def retry_run(self, run_id: str) -> dict:
        """Start a fresh run in the same chat for a failed/cancelled task, telling
        the model what already happened. Nothing from the old run is replayed."""
        old = self.runs.get(run_id)
        if old.state not in ("FAILED", "CANCELLED"):
            raise _Terminal("only failed or stopped tasks can be retried")
        plan = self._plans.get(run_id, {})
        done = [s["title"] for s in plan.get("steps", []) if s.get("status") == "done"]
        note = (f"[Runtime note — trusted] Retrying a task that stopped ({old.failure_code or old.state}: "
                f"{old.failure_message or 'stopped by the user'}). "
                + (f"Steps already completed: {'; '.join(done)}. " if done else "")
                + "Don't repeat external actions that already succeeded; continue from where it stopped.")
        text = self._run_text.get(run_id, "")
        run, created, msg_id = self.submit_message(
            chat_id=old.chat_id, user_id=old.user_id,
            content=[{"type": "text", "text": text}, {"type": "text", "text": note}],
            idempotency_key=f"retry:{run_id}:{int(time.time())}")
        self._run_text[run.run_id] = text
        return {"run_id": run.run_id, "chat_id": run.chat_id, "retry_of": run_id}

    def _record_receipt(self, run) -> None:
        log = self._logs.get(run.run_id)
        tools: dict[str, int] = {}
        approvals = []
        for e in (log.events if log else []):
            if e.type == "tool.result" and e.payload.get("status") == "succeeded":
                tools[e.payload.get("tool", "?")] = tools.get(e.payload.get("tool", "?"), 0) + 1
            elif e.type == "approval.decided":
                approvals.append({"approval_id": e.payload.get("approval_id"),
                                  "verdict": e.payload.get("verdict")})
        receipt = {"run_id": run.run_id, "chat_id": run.chat_id, "user_id": run.user_id,
                   "goal": self._run_text.get(run.run_id, "")[:300], "outcome": run.state,
                   "plan": self._plans.get(run.run_id), "tools": tools, "approvals": approvals,
                   "model_calls": run.model_calls_used, "tool_calls": run.tool_calls_used,
                   "duration_s": round(time.time() - run.created_at, 1),
                   "result": (run.final_text or run.failure_message or "")[:500],
                   "finished_at": time.time()}
        self._receipts[run.run_id] = receipt
        if self.db is not None:
            self.db.kv_put("receipts", run.run_id, receipt, user_id=run.user_id)

    def receipt(self, run_id: str) -> dict | None:
        if run_id in self._receipts:
            return self._receipts[run_id]
        return self.db.kv_get("receipts", run_id) if self.db is not None else None

    def activity(self, user_id: str, limit: int = 60) -> list[dict]:
        runs = [r for r in self.runs._runs.values() if r.user_id == user_id]
        runs.sort(key=lambda r: r.created_at, reverse=True)
        out = []
        for r in runs[:limit]:
            state = r.state
            bucket = ("waiting" if state in (WAITING_FOR_APPROVAL, "PAUSED") else
                      "done" if state == "COMPLETED" else
                      "failed" if state in ("FAILED", "CANCELLED") else "running")
            chat = self.sessions.get(r.chat_id)
            plan = self._plans.get(r.run_id) or (self.receipt(r.run_id) or {}).get("plan")
            out.append({"run_id": r.run_id, "chat_id": r.chat_id, "chat_title": chat.title if chat else "",
                        "state": state, "bucket": bucket, "created_at": r.created_at,
                        "text": self._run_text.get(r.run_id, "")[:160],
                        "steps_done": sum(1 for s in (plan or {}).get("steps", []) if s.get("status") == "done"),
                        "steps_total": len((plan or {}).get("steps", [])),
                        "failure": r.failure_message or ""})
        return out

    # -- internal event -> SSE translation ----------------------------------
    def _translate_new_events(self, run_id: str) -> None:
        lock = self._translate_locks.setdefault(run_id, threading.RLock())
        with lock:
            self._translate_locked(run_id)

    def _tool_args(self, run, call_id: str) -> dict:
        for m in reversed(run.messages):
            for tc in (m.tool_calls or []):
                if tc.id == call_id:
                    args = dict(tc.arguments or {})
                    for k, v in list(args.items()):
                        if isinstance(v, str) and len(v) > 200:
                            args[k] = v[:200] + "…"
                    return args
        return {}

    def _browser_event(self, run_id: str, run, t: str, p: dict) -> None:
        sid = p.get("session_id", "")
        if not sid:
            return
        self._browser_runs[sid] = run_id
        if t == "browser.session_started":
            self._chat_browser[run.chat_id] = sid
            self.eventbus.publish(run_id, "browser.session", {"session_id": sid, "state": "open"})
            return
        if t == "browser.session_closed":
            if self._chat_browser.get(run.chat_id) == sid:
                self._chat_browser.pop(run.chat_id, None)
            self.eventbus.publish(run_id, "browser.session", {"session_id": sid, "state": "closed"})
            return
        info = (self.browser.session_info(sid)
                if self.browser is not None and hasattr(self.browser, "session_info") else None) or {}
        last = info.get("last_action") or {}
        status = {"browser.commit_proposed": "commit_proposed",
                  "browser.challenge_detected": "challenge_paused",
                  "browser.denied": "refused"}.get(t, p.get("status", "ok"))
        self.eventbus.publish(run_id, "browser.action", {
            "session_id": sid, "kind": p.get("kind", t.split(".", 1)[1]),
            "status": status, "label": last.get("label", ""),
            "url": info.get("url", p.get("url", "")), "title": info.get("title", ""),
            "code": p.get("code"),
        })

    def _translate_locked(self, run_id: str) -> None:
        run = self.runs.get(run_id)
        log = self._logs[run_id]
        start = self._emitted.get(run_id, 0)
        last_requested: dict = self._last_requested.setdefault(run_id, {})
        for evt in log.events[start:]:
            self._emitted[run_id] = evt.sequence + 1
            p = evt.payload or {}
            t = evt.type
            if t == "run.transition":
                self.eventbus.publish(run_id, M.SSE_RUN_STATUS, {"status": p.get("to")})
            elif t == "policy.decisions":
                for d in p.get("decisions", []):
                    if d.get("decision") in ("ALLOW", "ASK", "DENY"):
                        self.eventbus.publish(run_id, "tool.call", {
                            "call_id": d.get("call_id"), "tool": d.get("tool"),
                            "decision": d.get("decision"), "reason": d.get("reason"),
                            "args": self._tool_args(run, d.get("call_id")),
                        })
            elif t.startswith("browser."):
                self._browser_event(run_id, run, t, p)
            elif t == "task.plan":
                self._plans[run_id] = {"goal": p.get("goal", ""), "steps": [dict(s) for s in p.get("steps", [])]}
                self.eventbus.publish(run_id, "task.plan", self._plans[run_id])
            elif t == "task.step":
                self._plans.setdefault(run_id, {"steps": []})["updated"] = True
                for s in self._plans.get(run_id, {}).get("steps", []):
                    if s["id"] == p.get("id"):
                        s["status"] = p.get("status")
                        s["note"] = p.get("note", "")
                self.eventbus.publish(run_id, "task.step", p)
            elif t == "task.input_required":
                self.eventbus.publish(run_id, "task.input_required", p)
            elif t == "run.paused":
                self.eventbus.publish(run_id, "run.paused", {"run_id": run_id})
            elif t == "tool.result":
                if p.get("tool") == "tools.load_namespace" and p.get("status") == "succeeded":
                    # the shared registry's loaded set is server-wide; make the
                    # namespace's schemas part of THIS run's next model request
                    ns = self._tool_args(run, p.get("call_id")).get("name")
                    if ns:
                        run.loaded_namespaces.add(ns)
                self.eventbus.publish(run_id, M.SSE_TOOL_RESULT, {
                    "call_id": p.get("call_id"), "tool": p.get("tool"),
                    "ok": p.get("status") == "succeeded",
                    **({"display": p["display"]} if p.get("display") else {}),
                })
            elif t == "approval.requested":
                last_requested.clear()
                last_requested.update(p)
            elif t == "approval.parked":
                aid = p.get("approval_id") or last_requested.get("approval_id", "")
                self._pending_approval[run_id] = aid
                self.notify(run.user_id, kind="approval", title="OpenMuse needs your OK",
                            body=f"Approve or deny: {last_requested.get('tool', 'an action')}",
                            link={"chat_id": run.chat_id, "run_id": run_id, "approval_id": aid},
                            dedupe_key=f"approval:{aid}")
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
                plan = self._plans.get(run_id)
                if plan and plan.get("steps") and not plan.get("updated"):
                    # The model finished without ticking its own checklist: a
                    # completed run delivered the plan, so close it out visibly.
                    for s in plan["steps"]:
                        if s.get("status") in (None, "pending", "active"):
                            s["status"] = "done"
                            self.eventbus.publish(run_id, "task.step", {"id": s["id"], "status": "done", "note": ""})
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
        self._save_run(self.runs.get(req.run_id))
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
