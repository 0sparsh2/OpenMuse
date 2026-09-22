"""memory.* tools — Phase 2 layered memory.

  memory.note   (R2, local_write) — journal + extract + consolidate a durable
                note. Same input/output schema as Phase 1.
  memory.recall (R1, none)        — hybrid semantic/lexical recall across
                curated memory, journal, and people pages.
  memory.forget (R2, local_write) — user-directed forgetting: conservative
                plan -> delete/tombstone -> regenerate projections -> verify.
                Ambiguous targets are never deleted; the plan is returned.

All three resolve a LayeredMemory rooted at <workspace_root>/.agent-memory/,
so memory is per-workspace (per-tenant in production).
"""
from __future__ import annotations

import os

from memory.layered import LayeredMemory

from tools.registry import ToolDefinition, ToolRegistry


def _layered(workspace_root: str) -> LayeredMemory:
    return LayeredMemory(os.path.join(workspace_root, ".agent-memory"))


def register(registry: ToolRegistry) -> None:
    registry.register_namespace("memory", "Durable user memory (Phase 2: layered memory).")

    def note(ctx, args):
        mem = _layered(ctx.workspace_root)
        result = mem.remember(args["note"].strip(), source_ref="memory.note")
        entry = mem.journal.get(result["journal_entry_id"])
        day_file = (os.path.join(mem.journal.root, entry.timestamp[:10] + ".md")
                    if entry else "")
        return {
            "stored": True,
            "path": os.path.relpath(day_file, ctx.workspace_root) if day_file else "",
            "memory_ids": result["memory_ids"],
            "ops": result["ops"],
        }

    registry.register(ToolDefinition(
        name="memory.note", version="2.0.0",
        description=(
            "Store a durable note about the user (preference, fact, commitment). "
            "The note is journaled; explicit durable statements are extracted "
            "into curated memory with semantic indexing. Use only for "
            "information that is durable and useful later."
        ),
        input_schema={"type": "object",
                      "properties": {"note": {"type": "string", "maxLength": 2000}},
                      "required": ["note"], "additionalProperties": False},
        output_schema={"type": "object",
                       "properties": {
                           "stored": {"type": "boolean"},
                           "path": {"type": "string"},
                           "memory_ids": {"type": "array", "items": {"type": "string"}},
                           "ops": {"type": "array"},
                       },
                       "required": ["stored", "path"]},
        capabilities=["memory.write"], side_effect="local_write", idempotency="keyed",
        default_timeout_ms=10_000, execute=note,
    ))

    def recall(ctx, args):
        mem = _layered(ctx.workspace_root)
        results = mem.recall(args["query"],
                             top_k=min(int(args.get("top_k", 5)), 10),
                             sources=args.get("sources") or None,
                             include_history=bool(args.get("include_history", False)))
        return {"results": results}

    registry.register(ToolDefinition(
        name="memory.recall", version="1.0.0",
        description=(
            "Recall relevant memories for the current request: curated facts, "
            "preferences, commitments, journal entries, and people. Search "
            "before answering about prior decisions, dates, people, "
            "preferences, commitments, or past events. Do not use for general "
            "knowledge unless personalization is relevant."
        ),
        input_schema={"type": "object",
                      "properties": {
                          "query": {"type": "string", "maxLength": 500},
                          "top_k": {"type": "integer", "minimum": 1, "maximum": 10,
                                    "default": 5},
                          "sources": {"type": "array",
                                      "items": {"type": "string",
                                                "enum": ["curated", "journal", "people"]}},
                          "include_history": {"type": "boolean", "default": False,
                                              "description": "Include superseded records."},
                      },
                      "required": ["query"], "additionalProperties": False},
        output_schema={"type": "object",
                       "properties": {"results": {"type": "array"}},
                       "required": ["results"]},
        capabilities=["memory.read"], side_effect="none", idempotency="pure",
        default_timeout_ms=10_000, execute=recall,
    ))

    def forget(ctx, args):
        mem = _layered(ctx.workspace_root)
        mode = args.get("mode", "tombstone")
        if mode not in ("tombstone", "delete"):
            mode = "tombstone"
        plan = mem.forgetting.plan(args["query"], mode=mode)
        if plan.status != "ready":
            return {"status": plan.status, "note": plan.note,
                    "targets": plan.targets, "removed": [], "verified": False}
        result = mem.forgetting.execute(plan)
        return {"status": result.status, "note": result.note,
                "targets": plan.targets, "removed": result.removed,
                "verified": result.verified}

    registry.register(ToolDefinition(
        name="memory.forget", version="1.0.0",
        description=(
            "User-directed forgetting: remove a memory (fact, journal entry, "
            "or person) and all its derivatives — vectors, projections, and "
            "index entries — then verify it no longer surfaces in recall. "
            "Only call when the user explicitly asks to forget something. "
            "Ambiguous targets are never deleted; a plan is returned instead."
        ),
        input_schema={"type": "object",
                      "properties": {
                          "query": {"type": "string", "maxLength": 500,
                                   "description": "What to forget."},
                          "mode": {"type": "string", "enum": ["tombstone", "delete"],
                                   "default": "tombstone",
                                   "description": "tombstone keeps a non-content "
                                                  "audit marker; delete removes the row."},
                      },
                      "required": ["query"], "additionalProperties": False},
        output_schema={"type": "object",
                       "properties": {
                           "status": {"type": "string"},
                           "note": {"type": "string"},
                           "targets": {"type": "array"},
                           "removed": {"type": "array"},
                           "verified": {"type": "boolean"},
                       },
                       "required": ["status", "removed", "verified"]},
        capabilities=["memory.forget"], side_effect="local_write", idempotency="keyed",
        default_timeout_ms=15_000, execute=forget,
    ))
