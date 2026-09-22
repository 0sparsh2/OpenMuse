"""
Working memory: turn-scoped scratchpad (short-term memory within a turn).

Holds the agent's own working notes — intermediate conclusions, extracted
candidates awaiting consolidation, open questions for this turn. It is
injected into the turn's context as a clearly delimited block and is
discarded when the turn ends; nothing here is durable.

Durable writes go through the memory tools, never through working memory.
"""
from __future__ import annotations


WORKING_MEMORY_HEADER = (
    "AGENT WORKING NOTES — this turn only, not durable. "
    "Do not treat these as user-confirmed facts."
)


class WorkingMemory:
    def __init__(self):
        self._notes: dict[str, str] = {}
        self._order: list[str] = []

    def set(self, key: str, value: str) -> None:
        if key not in self._notes:
            self._order.append(key)
        self._notes[key] = value

    def get(self, key: str, default: str = "") -> str:
        return self._notes.get(key, default)

    def append(self, key: str, value: str) -> None:
        prev = self._notes.get(key, "")
        self.set(key, (prev + "\n" + value).strip() if prev else value)

    def clear(self) -> None:
        self._notes.clear()
        self._order.clear()

    def __len__(self) -> int:
        return len(self._notes)

    def render(self) -> str:
        """Render as a delimited context block. Empty string when unused."""
        if not self._notes:
            return ""
        lines = [WORKING_MEMORY_HEADER, ""]
        for key in self._order:
            lines.append(f"- {key}: {self._notes[key]}")
        return "\n".join(lines)
