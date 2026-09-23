"""task.* tools — visible plans and questions for the user (issue #6).

  task.plan     (R0) — publish a short step list for a multi-step task.
  task.update   (R0) — mark one step active / done / failed / skipped.
  task.ask_user (R0) — ask the user a question (optionally with choices);
                       the client shows an answer card. End your turn after
                       asking — the user's answer arrives as the next message.

The tools only emit events (task.plan / task.step / task.input_required);
the API turns them into SSE for the live checklist and into the run receipt.
"""
from __future__ import annotations

from tools.registry import ToolDefinition, ToolRegistry

STATUSES = ("pending", "active", "done", "failed", "skipped")


def register(registry: ToolRegistry) -> None:
    registry.register_namespace("task", "Show the user a live plan and ask them questions.")

    def plan(ctx, args):
        steps = []
        for i, s in enumerate(args["steps"][:12]):
            title = s.get("title", "") if isinstance(s, dict) else str(s)
            steps.append({"id": str((s.get("id") if isinstance(s, dict) else None) or i + 1),
                          "title": title.strip()[:120], "status": "pending"})
        ctx.event_log.append("task.plan", {"steps": steps, "goal": str(args.get("goal", ""))[:200]})
        return {"ok": True, "steps": [s["id"] for s in steps]}

    registry.register(ToolDefinition(
        name="task.plan", version="1.0.0",
        description=("Show the user a short checklist (3-7 steps) for a task that needs "
                     "several actions. Call once at the start; then task.update as you go."),
        input_schema={"type": "object", "properties": {
            "goal": {"type": "string", "maxLength": 200},
            "steps": {"type": "array", "minItems": 1, "maxItems": 12, "items": {
                "anyOf": [{"type": "string", "maxLength": 120},
                          {"type": "object", "properties": {
                              "id": {"type": "string", "maxLength": 20},
                              "title": {"type": "string", "maxLength": 120}},
                           "required": ["title"]}]}}},
            "required": ["steps"], "additionalProperties": False},
        output_schema={"type": "object"},
        capabilities=["task.plan"], side_effect="none", idempotency="pure",
        default_timeout_ms=2_000, execute=plan,
    ))

    def update(ctx, args):
        status = args["status"] if args["status"] in STATUSES else "active"
        ctx.event_log.append("task.step", {"id": str(args["step"]), "status": status,
                                           "note": str(args.get("note", ""))[:200]})
        return {"ok": True}

    registry.register(ToolDefinition(
        name="task.update", version="1.0.0",
        description="Update one plan step: active when you start it, done/failed/skipped when finished.",
        input_schema={"type": "object", "properties": {
            "step": {"type": "string", "maxLength": 20},
            "status": {"type": "string", "enum": list(STATUSES)},
            "note": {"type": "string", "maxLength": 200}},
            "required": ["step", "status"], "additionalProperties": False},
        output_schema={"type": "object"},
        capabilities=["task.plan"], side_effect="none", idempotency="pure",
        default_timeout_ms=2_000, execute=update,
    ))

    def ask_user(ctx, args):
        options = [str(o)[:80] for o in (args.get("options") or [])][:6]
        ctx.event_log.append("task.input_required", {"question": args["question"][:400],
                                                     "options": options})
        return {"asked": True, "note": "Now end your turn with the question; the user's answer "
                                       "will arrive as their next message."}

    registry.register(ToolDefinition(
        name="task.ask_user", version="1.0.0",
        description=("Ask the user something you need to continue (e.g. which date, which option). "
                     "Give 2-6 short options when the choices are clear. After calling it, end your "
                     "turn by restating the question — don't guess the answer."),
        input_schema={"type": "object", "properties": {
            "question": {"type": "string", "maxLength": 400},
            "options": {"type": "array", "maxItems": 6, "items": {"type": "string", "maxLength": 80}}},
            "required": ["question"], "additionalProperties": False},
        output_schema={"type": "object"},
        capabilities=["task.ask"], side_effect="none", idempotency="pure",
        default_timeout_ms=2_000, execute=ask_user,
    ))
