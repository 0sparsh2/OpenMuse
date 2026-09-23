"""
Parallel subagents in the live app (issue #13).

subagent.parallel fans out 2-3 focused child tasks at once (e.g. "check
Google Flights / Kayak / the airline's own site") and returns their results
together. Children run through the existing SubagentRunner, so they keep its
guarantees: tools = parent's tools ∩ requested ceiling, budgets carved from
the parent (never additive), depth cap, and no external writes unless
explicitly delegated. They act for the parent's user in the parent's
workspace; their browser sessions are surfaced to the parent run so the user
can watch each child live.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from subagents.runner import DelegationRequest

MAX_PARALLEL = 3


def register_parallel(registry, runner) -> None:
    from tools.registry import ToolDefinition

    def parallel(ctx, args):
        tasks = [t for t in args["tasks"] if str(t).strip()][:MAX_PARALLEL]
        if len(tasks) < 2:
            raise ValueError("give 2-3 independent tasks (for one task, just do it yourself)")
        ceiling = list(args.get("capability_ceiling") or ["browser", "web", "memory", "docs"])
        parent_log = ctx.event_log
        ids = []
        for t in tasks:
            did = runner.spawn(DelegationRequest(
                task=str(t).strip()[:2000], allowed_namespaces=ceiling,
                budget_carve=dict(args.get("budget") or {}), max_depth=1,
                parent_run_id=ctx.run_id, output_contract={}, context_refs=[],
                join_policy="all", explicit_writes=[]), defer=True)
            ids.append(did)
            # surface each child's live browser in the parent run (UI watches it)
            child_log = runner._child_logs[did]
            child_log.on_append = (lambda evt, did=did: parent_log.append(
                evt.type, {**evt.payload, "delegation_id": did})
                if evt.type.startswith("browser.") else None)
        with ThreadPoolExecutor(max_workers=len(ids), thread_name_prefix="subagent") as pool:
            list(pool.map(lambda d: runner._drive_to_terminal(runner._delegations[d]), ids))
        out = []
        for d, t in zip(ids, tasks):
            st = runner.status(d)
            res = st.get("result") or {}
            text = (res.get("output") or {}).get("text") if isinstance(res.get("output"), dict) else None
            out.append({"task": t, "status": st.get("status"),
                        "result": (text or res.get("raw_text") or res.get("notes") or "")[:3000]})
        return {"results": out}

    registry.register(ToolDefinition(
        name="subagent.parallel", version="1.0.0",
        description=("Run 2-3 INDEPENDENT sub-tasks at the same time with focused helper agents and get all "
                     "their results back (e.g. compare prices on three different sites). Each helper only has "
                     "the tools in capability_ceiling (default: browser, web, memory, docs), can't send or "
                     "buy anything, and reports back to you. Use when parts don't depend on each other."),
        input_schema={"type": "object", "properties": {
            "tasks": {"type": "array", "minItems": 2, "maxItems": MAX_PARALLEL,
                      "items": {"type": "string", "maxLength": 1500}},
            "capability_ceiling": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
            "budget": {"type": "object", "properties": {
                "model_calls": {"type": "integer", "minimum": 1, "maximum": 20},
                "tool_calls": {"type": "integer", "minimum": 1, "maximum": 40},
                "wall_seconds": {"type": "integer", "minimum": 30, "maximum": 900}}}},
            "required": ["tasks"], "additionalProperties": False},
        output_schema={"type": "object"}, capabilities=["subagent.spawn"], side_effect="local_write",
        idempotency="unsafe_retry", default_timeout_ms=900_000, execute=parallel,
    ))
