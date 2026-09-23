"""
Tool registry with deferred namespaces.

The model initially receives only a compact namespace catalog
(name + description). It calls `tools.load_namespace` to pull a namespace's
full tool schemas into context. This two-stage discovery keeps the prompt
small and prevents irrelevant capabilities from competing during planning.

Mirrors blueprint "Tool runtime and deferred namespaces".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolDefinition:
    name: str  # "namespace.tool"
    version: str
    description: str
    input_schema: dict
    output_schema: dict
    capabilities: list[str]
    side_effect: str  # none | local_write | external_write | destructive
    idempotency: str  # pure | keyed | unsafe_retry
    default_timeout_ms: int
    data_classes_accepted: list[str] = field(default_factory=lambda: ["public", "personal"])
    execute: Callable[..., Any] | None = None  # (ctx, input) -> output dict
    # Optional: extra fields an approval must bind to (e.g. commit origin/amount).
    approval_bind_fields: Callable[[dict], dict] | None = None


class UnknownToolError(KeyError):
    pass


class UnknownNamespaceError(KeyError):
    pass


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._namespaces: dict[str, str] = {}  # namespace -> description

    # -- registration -----------------------------------------------------
    def register_namespace(self, name: str, description: str) -> None:
        self._namespaces[name] = description

    def register(self, tool: ToolDefinition) -> None:
        namespace = tool.name.split(".", 1)[0]
        if namespace not in self._namespaces:
            raise ValueError(f"namespace {namespace!r} is not registered")
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} already registered")
        self._validate_contract(tool)
        self._tools[tool.name] = tool

    @staticmethod
    def _validate_contract(tool: ToolDefinition) -> None:
        if "." not in tool.name:
            raise ValueError(f"tool name must be namespaced: {tool.name!r}")
        if tool.side_effect not in ("none", "local_write", "external_write", "destructive"):
            raise ValueError(f"bad side_effect: {tool.side_effect}")
        if tool.idempotency not in ("pure", "keyed", "unsafe_retry"):
            raise ValueError(f"bad idempotency: {tool.idempotency}")
        if not isinstance(tool.input_schema, dict) or tool.input_schema.get("type") != "object":
            raise ValueError("input_schema must be a JSON object schema")
        if tool.execute is None:
            raise ValueError(f"tool {tool.name!r} has no executor")

    # -- discovery --------------------------------------------------------
    def catalog(self) -> list[dict]:
        """Compact namespace catalog handed to the model on every turn."""
        return [
            {"name": name, "description": desc}
            for name, desc in sorted(self._namespaces.items())
        ]

    def namespace_names(self) -> list[str]:
        return sorted(self._namespaces)

    def load_namespace(self, name: str) -> list[dict]:
        """Full tool schemas for one namespace. Raises UnknownNamespaceError."""
        if name not in self._namespaces:
            raise UnknownNamespaceError(name)
        prefix = f"{name}."
        return [
            {
                "name": t.name,
                "version": t.version,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in self._tools.values()
            if t.name.startswith(prefix)
        ]

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownToolError(name) from None

    def tool_names(self) -> list[str]:
        return sorted(self._tools)
