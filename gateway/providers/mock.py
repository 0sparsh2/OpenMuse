"""
Mock providers for smoke tests and evaluations.

No network, no API key, deterministic. The mock never invents tool names:
ScriptedMockProvider plays a fixed script; ProgrammableMockProvider delegates
each step to a caller-supplied function so demos can react to tool results.
"""
from __future__ import annotations

from typing import Callable, Optional

from gateway.protocol import (
    ChatMessage,
    ModelRequest,
    ModelResponse,
    Provider,
    ToolCall,
)


class ScriptedMockProvider(Provider):
    """Plays a fixed list of responses in order, then repeats the last."""

    name = "mock-scripted"

    def __init__(self, script: list[dict]):
        """
        script entries: {"text": str} or {"text": str, "tool_calls": [{"name":..., "arguments": {...}}]}
        """
        if not script:
            raise ValueError("script must not be empty")
        self.script = script
        self.calls: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        step = min(len(self.calls) - 1, len(self.script) - 1)
        entry = self.script[step]
        tool_calls = [
            ToolCall(
                id=f"call_mock_{len(self.calls)}_{i}",
                name=tc["name"],
                arguments=dict(tc.get("arguments", {})),
            )
            for i, tc in enumerate(entry.get("tool_calls", []))
        ]
        return ModelResponse(
            text=entry.get("text", ""),
            tool_calls=tool_calls,
            stop_reason="tool_calls" if tool_calls else "stop",
            usage={"input_tokens": 0, "output_tokens": 0},
        )


class ProgrammableMockProvider(Provider):
    """
    Calls `respond(request, history)` on every step, where history is the list
    of prior requests. Lets a demo compose a final answer from tool results
    while staying fully deterministic and offline.
    """

    name = "mock-programmable"

    def __init__(self, respond: Callable[[ModelRequest, list[ModelRequest]], ModelResponse]):
        self.respond = respond
        self.calls: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        history = list(self.calls)
        self.calls.append(request)
        return self.respond(request, history)


def find_tool_results(request: ModelRequest) -> list[ChatMessage]:
    """Helper for programmable mocks: pull tool-role messages from a request."""
    return [m for m in request.messages if m.role == "tool"]
