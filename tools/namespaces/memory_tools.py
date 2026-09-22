"""memory.note — Phase 1 durable note write.

This is the seam for the Phase 2 layered memory system (curated durable
memory, episodic journal, knowledge bank, people/groups, forgetting). Phase 1
stores plain timestamped notes in <workspace>/.agent-memory/notes.md; Phase 2
replaces the store behind this tool's contract without changing its schema.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from tools.registry import ToolDefinition, ToolRegistry


def _notes_path(workspace_root: str) -> str:
    d = os.path.join(workspace_root, ".agent-memory")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "notes.md")


def register(registry: ToolRegistry) -> None:
    registry.register_namespace("memory", "Durable user memory (Phase 1: plain notes).")

    def note(ctx, args):
        path = _notes_path(ctx.workspace_root)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = f"\n## {stamp}\n{args['note'].strip()}\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(entry)
        return {"stored": True, "path": os.path.relpath(path, ctx.workspace_root)}

    registry.register(ToolDefinition(
        name="memory.note", version="1.0.0",
        description=(
            "Store a durable note about the user (preference, fact, commitment). "
            "Use only for information that is durable and useful later."
        ),
        input_schema={"type": "object",
                      "properties": {"note": {"type": "string", "maxLength": 2000}},
                      "required": ["note"], "additionalProperties": False},
        output_schema={"type": "object",
                       "properties": {"stored": {"type": "boolean"}, "path": {"type": "string"}},
                       "required": ["stored", "path"]},
        capabilities=["memory.write"], side_effect="local_write", idempotency="keyed",
        default_timeout_ms=5_000, execute=note,
    ))
