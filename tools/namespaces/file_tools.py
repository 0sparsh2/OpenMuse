"""files.* — path-scoped file tools.

All paths are jailed to the run's workspace root: absolute paths, `..`
segments, and symlinks that escape the root are rejected. This is the
"local file write is auditable and path-scoped" exit criterion.
"""
from __future__ import annotations

import os

from tools.registry import ToolDefinition, ToolRegistry

MAX_READ_BYTES = 200_000


def _resolve(root: str, path: str) -> str:
    if os.path.isabs(path):
        raise ValueError(f"absolute paths are not allowed: {path!r}")
    joined = os.path.normpath(os.path.join(root, path))
    real_root = os.path.realpath(root)
    real_joined = os.path.realpath(joined)
    if real_joined != real_root and not real_joined.startswith(real_root + os.sep):
        raise ValueError(f"path escapes the workspace: {path!r}")
    return joined


def register(registry: ToolRegistry) -> None:
    registry.register_namespace("files", "Read, write, and list files inside the run workspace.")

    def read(ctx, args):
        full = _resolve(ctx.workspace_root, args["path"])
        if not os.path.isfile(full):
            raise ValueError(f"no such file: {args['path']!r}")
        size = os.path.getsize(full)
        truncated = size > MAX_READ_BYTES
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read(MAX_READ_BYTES)
        return {"path": args["path"], "content": content, "truncated": truncated, "bytes": size}

    def write(ctx, args):
        full = _resolve(ctx.workspace_root, args["path"])
        os.makedirs(os.path.dirname(full) or ctx.workspace_root, exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(args["content"])
        return {"path": args["path"], "bytes_written": len(args["content"].encode("utf-8"))}

    def list_dir(ctx, args):
        full = _resolve(ctx.workspace_root, args.get("path", "."))
        if not os.path.isdir(full):
            raise ValueError(f"no such directory: {args.get('path', '.')!r}")
        entries = sorted(os.listdir(full))
        return {"path": args.get("path", "."), "entries": entries[:500]}

    registry.register(ToolDefinition(
        name="files.read", version="1.0.0",
        description="Read a UTF-8 text file from the workspace. Read before writing.",
        input_schema={"type": "object",
                      "properties": {"path": {"type": "string", "maxLength": 500}},
                      "required": ["path"], "additionalProperties": False},
        output_schema={"type": "object",
                       "properties": {"path": {"type": "string"}, "content": {"type": "string"},
                                      "truncated": {"type": "boolean"}, "bytes": {"type": "integer"}},
                       "required": ["path", "content", "truncated", "bytes"]},
        capabilities=["filesystem.read.scoped"], side_effect="none", idempotency="pure",
        default_timeout_ms=5_000, execute=read,
    ))
    registry.register(ToolDefinition(
        name="files.write", version="1.0.0",
        description="Write (create or overwrite) a UTF-8 text file in the workspace.",
        input_schema={"type": "object",
                      "properties": {"path": {"type": "string", "maxLength": 500},
                                     "content": {"type": "string", "maxLength": 500_000}},
                      "required": ["path", "content"], "additionalProperties": False},
        output_schema={"type": "object",
                       "properties": {"path": {"type": "string"}, "bytes_written": {"type": "integer"}},
                       "required": ["path", "bytes_written"]},
        capabilities=["filesystem.write.scoped"], side_effect="local_write", idempotency="keyed",
        default_timeout_ms=5_000, execute=write,
    ))
    registry.register(ToolDefinition(
        name="files.list", version="1.0.0",
        description="List entries in a workspace directory.",
        input_schema={"type": "object",
                      "properties": {"path": {"type": "string", "maxLength": 500, "default": "."}},
                      "additionalProperties": False},
        output_schema={"type": "object",
                       "properties": {"path": {"type": "string"}, "entries": {"type": "array"}},
                       "required": ["path", "entries"]},
        capabilities=["filesystem.read.scoped"], side_effect="none", idempotency="pure",
        default_timeout_ms=5_000, execute=list_dir,
    ))
