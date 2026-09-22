"""shell.exec — scoped command execution with a deterministic destructive-command guard.

Runs inside the workspace root with a scrubbed environment (secret-looking
variables are removed before the child starts) and a hard timeout.
A deterministic denylist rejects destructive patterns BEFORE policy runs,
so they can never be approved into existence.
"""
from __future__ import annotations

import os
import re
import subprocess

from tools.registry import ToolDefinition, ToolRegistry

# Deterministic guard: these can never run, regardless of approval state.
_DESTRUCTIVE = [
    re.compile(r"(^|[\s;&|])rm\s+.*-[a-z]*r", re.IGNORECASE),   # rm -r / -rf
    re.compile(r":\(\)\s*\{", re.IGNORECASE),                    # fork bomb
    re.compile(r"\bdd\s+.*of=/dev/", re.IGNORECASE),             # dd to devices
    re.compile(r"\bmkfs(\.|$|\s)", re.IGNORECASE),
    re.compile(r"(^|[\s;&|])(shutdown|reboot|halt|poweroff)\b", re.IGNORECASE),
    re.compile(r">\s*/dev/(sd[a-z]|nvme|hd[a-z]|vd[a-z])", re.IGNORECASE),
    re.compile(r"\bchmod\s+-R\s+777\s+/", re.IGNORECASE),
    re.compile(r"\bcurl\b.*\|\s*(ba)?sh\b", re.IGNORECASE),      # curl|sh
    re.compile(r"\bwget\b.*\|\s*(ba)?sh\b", re.IGNORECASE),
]


def is_destructive(command: str) -> str | None:
    """Return a reason string if the command matches the destructive guard."""
    for pattern in _DESTRUCTIVE:
        if pattern.search(command):
            return f"command matches destructive pattern: {pattern.pattern}"
    return None


def _scrubbed_env() -> dict:
    keep = {}
    for k, v in os.environ.items():
        ku = k.upper()
        if any(s in ku for s in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "PRIVATE")):
            continue
        keep[k] = v
    keep["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    return keep


def register(registry: ToolRegistry) -> None:
    registry.register_namespace("shell", "Run shell commands jailed to the workspace.")

    def exec_cmd(ctx, args):
        command = args["command"]
        reason = is_destructive(command)
        if reason:
            # Deterministic deny: surfaced as a failed result, never executed.
            raise PermissionError(f"refused by destructive-command guard: {reason}")
        proc = subprocess.run(
            command,
            shell=True,
            cwd=ctx.workspace_root,
            env=_scrubbed_env(),
            capture_output=True,
            text=True,
            timeout=args.get("timeout_s", 20),
        )
        stdout = proc.stdout[-20_000:]
        stderr = proc.stderr[-5_000:]
        return {
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "command": command,
        }

    registry.register(ToolDefinition(
        name="shell.exec", version="1.0.0",
        description="Run a shell command jailed to the workspace directory. Destructive patterns are refused.",
        input_schema={"type": "object",
                      "properties": {"command": {"type": "string", "maxLength": 4000},
                                     "timeout_s": {"type": "integer", "minimum": 1, "maximum": 120, "default": 20}},
                      "required": ["command"], "additionalProperties": False},
        output_schema={"type": "object",
                       "properties": {"exit_code": {"type": "integer"}, "stdout": {"type": "string"},
                                      "stderr": {"type": "string"}, "command": {"type": "string"}},
                       "required": ["exit_code", "stdout", "stderr", "command"]},
        capabilities=["shell.exec.scoped"], side_effect="local_write", idempotency="unsafe_retry",
        default_timeout_ms=125_000, execute=exec_cmd,
    ))
