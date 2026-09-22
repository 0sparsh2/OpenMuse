"""system.* and tools.* — always-available utilities."""
from __future__ import annotations

from datetime import datetime, timezone

from tools.registry import ToolDefinition, ToolRegistry


def register(registry: ToolRegistry, *, loaded_namespaces: set[str]) -> None:
    registry.register_namespace("system", "Clock and runtime information.")
    registry.register_namespace("tools", "Tool discovery: load a namespace's schemas.")

    def clock(ctx, args):
        now = datetime.now(timezone.utc)
        return {"iso": now.isoformat(), "unix": int(now.timestamp())}

    registry.register(ToolDefinition(
        name="system.clock", version="1.0.0",
        description="Current date and time (UTC ISO-8601 plus unix epoch).",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={"type": "object",
                       "properties": {"iso": {"type": "string"}, "unix": {"type": "integer"}},
                       "required": ["iso", "unix"]},
        capabilities=["time.read"], side_effect="none", idempotency="pure",
        default_timeout_ms=2_000, execute=clock,
    ))

    def load_namespace(ctx, args):
        from tools.registry import UnknownNamespaceError

        name = args["name"]
        try:
            schemas = registry.load_namespace(name)
        except UnknownNamespaceError:
            raise ValueError(
                f"Unknown namespace {name!r}. Known namespaces: "
                f"{', '.join(registry.namespace_names())}."
            )
        loaded_namespaces.add(name)
        return {"namespace": name, "tools": schemas}

    registry.register(ToolDefinition(
        name="tools.load_namespace", version="1.0.0",
        description=(
            "Load a tool namespace's full schemas into context. "
            "Call this before using tools from a namespace you have not loaded yet."
        ),
        input_schema={"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"], "additionalProperties": False},
        output_schema={"type": "object",
                       "properties": {"namespace": {"type": "string"},
                                      "tools": {"type": "array"}},
                       "required": ["namespace", "tools"]},
        capabilities=["tools.discover"], side_effect="none", idempotency="pure",
        default_timeout_ms=2_000, execute=load_namespace,
    ))
