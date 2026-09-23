"""
SQLite persistence for the API (issue #2).

One file (WAL mode) holds chats, runs (including their message history),
the SSE event stream, and approvals, so a restart loses nothing a user can
see: chat lists and transcripts reload, reconnecting clients replay events,
and a run parked on an approval can still be approved and resumed.

The backend writes through at the moments that matter (chat created, run
started / parked / finished, every SSE event, every approval change); on boot
it loads everything back. Runs that were mid-flight when the process died
are marked FAILED (WORKER_RESTARTED) — never replayed, so no side-effecting
tool can run twice.

Everything is plain JSON in TEXT columns; the schema avoids SQLite-only
features so a Postgres implementation can sit behind the same methods.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chats (
  chat_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS chats_by_user ON chats(user_id, archived, updated_at);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, user_id TEXT NOT NULL,
  state TEXT NOT NULL, user_text TEXT NOT NULL DEFAULT '', snapshot TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS runs_by_chat ON runs(chat_id, created_at);
CREATE INDEX IF NOT EXISTS runs_by_user ON runs(user_id, updated_at);
CREATE TABLE IF NOT EXISTS sse_events (
  run_id TEXT NOT NULL, seq INTEGER NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL,
  occurred_at REAL NOT NULL, PRIMARY KEY (run_id, seq));
CREATE TABLE IF NOT EXISTS approvals (
  approval_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS approvals_by_run ON approvals(run_id);
CREATE TABLE IF NOT EXISTS kv (
  ns TEXT NOT NULL, key TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '',
  data TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (ns, key));
CREATE INDEX IF NOT EXISTS kv_by_user ON kv(ns, user_id, updated_at);
"""
SCHEMA_VERSION = "1"


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _exec(self, sql: str, args: tuple = ()):
        with self._lock:
            return self._db.execute(sql, args)

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # -- chats ------------------------------------------------------------------
    def save_chat(self, rec) -> None:
        now = time.time()
        self._exec("""INSERT INTO chats(chat_id, tenant_id, user_id, title, archived, created_at, updated_at)
                      VALUES (?,?,?,?,0,?,?)
                      ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, updated_at=excluded.updated_at""",
                   (rec.chat_id, rec.tenant_id, rec.user_id, rec.title or "", rec.created_at, now))

    def update_chat(self, chat_id: str, *, title: str | None = None,
                    archived: bool | None = None, touch: bool = True) -> None:
        sets, args = [], []
        if title is not None:
            sets.append("title=?"); args.append(title)
        if archived is not None:
            sets.append("archived=?"); args.append(1 if archived else 0)
        if touch:
            sets.append("updated_at=?"); args.append(time.time())
        if sets:
            self._exec(f"UPDATE chats SET {', '.join(sets)} WHERE chat_id=?", (*args, chat_id))

    def chats(self) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM chats")]

    def list_chats(self, user_id: str, *, archived: bool = False, limit: int = 100) -> list[dict]:
        rows = self._all("""SELECT c.*, (SELECT user_text FROM runs r WHERE r.chat_id=c.chat_id
                                         ORDER BY created_at DESC LIMIT 1) AS last_user_text,
                                   (SELECT COUNT(*) FROM runs r WHERE r.chat_id=c.chat_id) AS turns
                            FROM chats c WHERE user_id=? AND archived=? ORDER BY updated_at DESC LIMIT ?""",
                         (user_id, 1 if archived else 0, limit))
        return [dict(r) for r in rows]

    # -- runs ---------------------------------------------------------------------
    def save_run(self, run, user_text: str = "") -> None:
        snap = run_to_dict(run)
        self._exec("""INSERT INTO runs(run_id, chat_id, user_id, state, user_text, snapshot, created_at, updated_at)
                      VALUES (?,?,?,?,?,?,?,?)
                      ON CONFLICT(run_id) DO UPDATE SET state=excluded.state, snapshot=excluded.snapshot,
                        updated_at=excluded.updated_at,
                        user_text=CASE WHEN excluded.user_text != '' THEN excluded.user_text ELSE runs.user_text END""",
                   (run.run_id, run.chat_id, run.user_id, run.state, user_text,
                    json.dumps(snap, ensure_ascii=False), run.created_at, time.time()))

    def runs(self) -> list[tuple[dict, str]]:
        return [(json.loads(r["snapshot"]), r["user_text"])
                for r in self._all("SELECT snapshot, user_text FROM runs ORDER BY created_at")]

    def chat_runs(self, chat_id: str) -> list[dict]:
        return [dict(r) for r in self._all(
            "SELECT run_id, state, user_text, snapshot, created_at FROM runs WHERE chat_id=? ORDER BY created_at",
            (chat_id,))]

    def user_runs(self, user_id: str, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self._all(
            "SELECT run_id, chat_id, state, user_text, snapshot, created_at, updated_at FROM runs "
            "WHERE user_id=? ORDER BY updated_at DESC LIMIT ?", (user_id, limit))]

    # -- SSE events -----------------------------------------------------------------
    def append_event(self, run_id: str, seq: int, type_: str, data: dict, at: float) -> None:
        self._exec("INSERT OR IGNORE INTO sse_events VALUES (?,?,?,?,?)",
                   (run_id, seq, type_, json.dumps(data, ensure_ascii=False), at))

    def events(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM sse_events ORDER BY run_id, seq")

    # -- approvals --------------------------------------------------------------------
    def save_approval(self, kind: str, obj) -> None:
        d = asdict(obj)
        key = d.get("id")
        self._exec("""INSERT INTO approvals VALUES (?,?,?,?)
                      ON CONFLICT(approval_id) DO UPDATE SET data=excluded.data""",
                   (key, d.get("run_id", ""), kind, json.dumps(d, ensure_ascii=False)))

    def approvals(self) -> list[tuple[str, dict]]:
        return [(r["kind"], json.loads(r["data"])) for r in self._all("SELECT kind, data FROM approvals")]

    # -- generic per-user documents (notifications, receipts, schedules…) -------------
    def kv_put(self, ns: str, key: str, data: dict, user_id: str = "") -> None:
        self._exec("""INSERT INTO kv VALUES (?,?,?,?,?)
                      ON CONFLICT(ns, key) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at""",
                   (ns, key, user_id, json.dumps(data, ensure_ascii=False), time.time()))

    def kv_get(self, ns: str, key: str) -> dict | None:
        rows = self._all("SELECT data FROM kv WHERE ns=? AND key=?", (ns, key))
        return json.loads(rows[0]["data"]) if rows else None

    def kv_list(self, ns: str, user_id: str | None = None, limit: int = 500) -> list[dict]:
        if user_id is None:
            rows = self._all("SELECT data FROM kv WHERE ns=? ORDER BY updated_at DESC LIMIT ?", (ns, limit))
        else:
            rows = self._all("SELECT data FROM kv WHERE ns=? AND user_id=? ORDER BY updated_at DESC LIMIT ?",
                             (ns, user_id, limit))
        return [json.loads(r["data"]) for r in rows]

    def kv_delete(self, ns: str, key: str) -> None:
        self._exec("DELETE FROM kv WHERE ns=? AND key=?", (ns, key))


# -- Run (de)serialization -------------------------------------------------------------
def run_to_dict(run) -> dict:
    return {
        "run_id": run.run_id, "tenant_id": run.tenant_id, "chat_id": run.chat_id,
        "user_id": run.user_id, "state": run.state,
        "messages": [_msg_to_dict(m) for m in run.messages],
        "loaded_namespaces": sorted(run.loaded_namespaces),
        "budgets": asdict(run.budgets),
        "model_calls_used": run.model_calls_used, "tool_calls_used": run.tool_calls_used,
        "step": run.step, "failure_code": run.failure_code,
        "failure_message": run.failure_message, "final_text": run.final_text,
        "idempotency_key": run.idempotency_key, "created_at": run.created_at,
    }


def run_from_dict(d: dict):
    from agent.models import Run, RunBudgets
    run = Run(run_id=d["run_id"], tenant_id=d["tenant_id"], chat_id=d["chat_id"],
              user_id=d["user_id"], state=d["state"],
              messages=[_msg_from_dict(m) for m in d.get("messages", [])],
              loaded_namespaces=set(d.get("loaded_namespaces", [])),
              budgets=RunBudgets(**d.get("budgets", {})),
              model_calls_used=d.get("model_calls_used", 0),
              tool_calls_used=d.get("tool_calls_used", 0), step=d.get("step", 0),
              failure_code=d.get("failure_code", ""), failure_message=d.get("failure_message", ""),
              final_text=d.get("final_text", ""), idempotency_key=d.get("idempotency_key", ""),
              created_at=d.get("created_at", time.time()))
    return run


def _msg_to_dict(m) -> dict:
    return {"role": m.role, "blocks": [asdict(b) for b in m.blocks],
            "tool_calls": [asdict(t) for t in m.tool_calls],
            "tool_call_id": m.tool_call_id, "name": m.name}


def _msg_from_dict(d: dict):
    from gateway.protocol import Block, ChatMessage, ToolCall
    return ChatMessage(role=d["role"], blocks=[Block(**b) for b in d.get("blocks", [])],
                       tool_calls=[ToolCall(**t) for t in d.get("tool_calls", [])],
                       tool_call_id=d.get("tool_call_id", ""), name=d.get("name", ""))
