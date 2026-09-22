"""subagents.* tools — Phase 3 delegation primitives.

  subagent.spawn  (R2, local_write) — spawn a bounded child with a task brief,
                  capability ceiling, carved budget, and output contract.
  subagent.list   (R0, none)        — list delegations for a parent run.
  subagent.status (R0, none)        — status + latest result of one delegation.
  subagent.send   (R2, local_write) — follow-up input to a settled child; runs
                  one more refinement pass on its REMAINING budget.
  subagent.close  (R1, none)        — close a delegation.

The namespace is registered with the parent run's SubagentRunner; tool calls
resolve the parent run from ctx.run_id. Children at max depth do not see this
namespace at all (delegation loops are blocked at spawn time).
"""
from __future__ import annotations

from agent.seams import DelegationRequest

from subagents.models import JoinPolicy
from subagents.runner import ParentRunInfo, SubagentRunner
from tools.registry import ToolDefinition, ToolRegistry


def register(registry: ToolRegistry, runner: SubagentRunner) -> None:
    registry.register_namespace(
        "subagent", "Delegate bounded work to child agents (Phase 3).")

    def spawn(ctx, args):
        req = DelegationRequest(
            task=args["objective"].strip(),
            allowed_namespaces=list(args.get("capability_ceiling", [])),
            budget_carve=dict(args.get("budget", {})),
            max_depth=int(args.get("max_depth", 2)),
            parent_run_id=ctx.run_id,
            output_contract=dict(args.get("output_contract", {})),
            context_refs=list(args.get("context_refs", [])),
            join_policy=str(args.get("join_policy", JoinPolicy.ALL)),
            explicit_writes=list(args.get("explicit_writes", [])),
        )
        delegation_id = runner.spawn(req)
        return {"delegation_id": delegation_id,
                "status": runner.status(delegation_id)}

    registry.register(ToolDefinition(
        name="subagent.spawn", version="1.0.0",
        description=(
            "Spawn a bounded child agent for a focused objective. The child "
            "receives ONLY the objective plus the given context_refs — never "
            "your full memory. Its tools are limited to capability_ceiling "
            "(intersection with your own), its budget is carved from yours, "
            "and it returns a typed result per output_contract. Use for "
            "independent research, analysis, or drafting subtasks."),
        input_schema={"type": "object",
                      "properties": {
                          "objective": {"type": "string", "maxLength": 2000},
                          "capability_ceiling": {
                              "type": "array", "items": {"type": "string"},
                              "description": "Namespace shorthands (e.g. 'files') "
                                             "or capabilities (e.g. 'filesystem.read.scoped')"},
                          "budget": {"type": "object",
                                     "properties": {
                                         "model_calls": {"type": "integer", "minimum": 1},
                                         "tool_calls": {"type": "integer", "minimum": 1},
                                         "wall_seconds": {"type": "integer", "minimum": 10}},
                                     "additionalProperties": False},
                          "output_contract": {"type": "object"},
                          "context_refs": {"type": "array", "items": {"type": "object"}},
                          "join_policy": {"type": "string",
                                          "enum": ["all", "any", "quorum"], "default": "all"},
                          "explicit_writes": {"type": "array", "items": {"type": "string"}},
                          "max_depth": {"type": "integer", "minimum": 1, "maximum": 2,
                                        "default": 2},
                      },
                      "required": ["objective"], "additionalProperties": False},
        output_schema={"type": "object",
                       "properties": {"delegation_id": {"type": "string"},
                                      "status": {"type": "object"}},
                       "required": ["delegation_id"]},
        capabilities=["subagent.spawn"], side_effect="local_write",
        idempotency="keyed", default_timeout_ms=120_000, execute=spawn,
    ))

    def list_delegations(ctx, args):
        return {"delegations": runner.list(ctx.run_id)}

    registry.register(ToolDefinition(
        name="subagent.list", version="1.0.0",
        description="List your child delegations and their statuses.",
        input_schema={"type": "object", "properties": {},
                      "additionalProperties": False},
        output_schema={"type": "object",
                       "properties": {"delegations": {"type": "array"}},
                       "required": ["delegations"]},
        capabilities=["subagent.read"], side_effect="none",
        idempotency="pure", default_timeout_ms=5_000, execute=list_delegations,
    ))

    def status(ctx, args):
        return runner.status(args["delegation_id"])

    registry.register(ToolDefinition(
        name="subagent.status", version="1.0.0",
        description="Status and latest typed result of one child delegation.",
        input_schema={"type": "object",
                      "properties": {"delegation_id": {"type": "string"}},
                      "required": ["delegation_id"], "additionalProperties": False},
        output_schema={"type": "object"},
        capabilities=["subagent.read"], side_effect="none",
        idempotency="pure", default_timeout_ms=5_000, execute=status,
    ))

    def send(ctx, args):
        return runner.send(args["delegation_id"], args["message"])

    registry.register(ToolDefinition(
        name="subagent.send", version="1.0.0",
        description=(
            "Send follow-up input to a settled child delegation. The child "
            "runs one more refinement pass on its REMAINING budget only."),
        input_schema={"type": "object",
                      "properties": {"delegation_id": {"type": "string"},
                                     "message": {"type": "string", "maxLength": 2000}},
                      "required": ["delegation_id", "message"],
                      "additionalProperties": False},
        output_schema={"type": "object"},
        capabilities=["subagent.write"], side_effect="local_write",
        idempotency="keyed", default_timeout_ms=120_000, execute=send,
    ))

    def close(ctx, args):
        return runner.close(args["delegation_id"])

    registry.register(ToolDefinition(
        name="subagent.close", version="1.0.0",
        description="Close a child delegation and release its resources.",
        input_schema={"type": "object",
                      "properties": {"delegation_id": {"type": "string"}},
                      "required": ["delegation_id"], "additionalProperties": False},
        output_schema={"type": "object",
                       "properties": {"delegation_id": {"type": "string"},
                                      "closed": {"type": "boolean"}},
                       "required": ["delegation_id", "closed"]},
        capabilities=["subagent.write"], side_effect="none",
        idempotency="keyed", default_timeout_ms=5_000, execute=close,
    ))
