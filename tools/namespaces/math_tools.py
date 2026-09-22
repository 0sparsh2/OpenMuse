"""math.calc — safe arithmetic evaluation (no eval of arbitrary code)."""
from __future__ import annotations

import ast
import operator

from tools.registry import ToolDefinition, ToolRegistry

_ALLOWED = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED:
        return _ALLOWED[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED:
        return _ALLOWED[type(node.op)](_eval(node.operand))
    raise ValueError(f"unsupported expression element: {ast.dump(node)}")


def register(registry: ToolRegistry) -> None:
    registry.register_namespace("math", "Safe arithmetic evaluation.")

    def calc(ctx, args):
        try:
            tree = ast.parse(args["expression"], mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"could not parse expression: {exc}")
        result = _eval(tree.body)
        if isinstance(result, float) and (result != result or abs(result) == float("inf")):
            raise ValueError("non-finite result")
        return {"result": result}

    registry.register(ToolDefinition(
        name="math.calc", version="1.0.0",
        description="Evaluate an arithmetic expression (+, -, *, /, //, %, **, parentheses).",
        input_schema={"type": "object",
                      "properties": {"expression": {"type": "string", "maxLength": 500}},
                      "required": ["expression"], "additionalProperties": False},
        output_schema={"type": "object",
                       "properties": {"result": {"type": "number"}},
                       "required": ["result"]},
        capabilities=["math.eval"], side_effect="none", idempotency="pure",
        default_timeout_ms=2_000, execute=calc,
    ))
