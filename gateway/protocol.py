"""
Model gateway protocol — the substitution point for the closed model family.

Agent code calls one interface regardless of provider. Provider adapters
convert the normalized request into the vendor's message format, preserve
tool-call identifiers, and map errors into stable classes.

Mirrors blueprint section "Model gateway: replacing Muse Spark".
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Error taxonomy (stable across providers)
# ---------------------------------------------------------------------------
TRANSIENT = "TRANSIENT"
RATE_LIMIT = "RATE_LIMIT"
INVALID_REQUEST = "INVALID_REQUEST"
CONTEXT_OVERFLOW = "CONTEXT_OVERFLOW"
AUTH_FAILURE = "AUTH_FAILURE"
SAFETY_BLOCK = "SAFETY_BLOCK"

ERROR_CODES = {
    TRANSIENT,
    RATE_LIMIT,
    INVALID_REQUEST,
    CONTEXT_OVERFLOW,
    AUTH_FAILURE,
    SAFETY_BLOCK,
}


class ProviderError(Exception):
    """A normalized provider failure. `retryable` is decided by the adapter."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown provider error code: {code}")
        super().__init__(message)
        self.code = code
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Normalized message model — typed separation of instructions and data.
# Adapters flatten these into the vendor's format; they must never merge
# untrusted data blocks into instruction fields.
# ---------------------------------------------------------------------------
@dataclass
class Block:
    kind: str  # "text" | "data"
    text: str = ""
    # data-block provenance (blueprint: context contamination rules)
    source: str = ""        # e.g. "tool:files.read", "web", "user_file"
    source_ref: str = ""    # e.g. call id, URL, file path
    trust: str = "system"   # system | user | reviewed_skill | untrusted
    sensitivity: str = "public"  # public | personal | sensitive | secret


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ChatMessage:
    role: str  # system | user | assistant | tool
    blocks: list[Block] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)  # assistant only
    tool_call_id: str = ""  # tool role only
    name: str = ""          # tool role only


@dataclass
class ToolSchema:
    name: str
    version: str
    description: str
    input_schema: dict


@dataclass
class RequestMetadata:
    tenant_id: str
    run_id: str
    step: int
    prompt_version: str
    data_class: str = "personal"  # public | personal | sensitive


@dataclass
class ModelRequest:
    request_id: str
    model_class: str  # planner | fast | vision | safety
    messages: list[ChatMessage]
    tools: list[ToolSchema] = field(default_factory=list)
    max_output_tokens: int = 2048
    temperature: float = 0.2
    metadata: Optional[RequestMetadata] = None


@dataclass
class ModelResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    usage: dict = field(default_factory=dict)  # input_tokens, output_tokens, cost_usd_micros


class Provider(abc.ABC):
    """A model provider behind the gateway."""

    name: str = "base"

    @abc.abstractmethod
    def complete(self, request: ModelRequest) -> ModelResponse:
        """Run one completion. May raise ProviderError."""
        raise NotImplementedError
