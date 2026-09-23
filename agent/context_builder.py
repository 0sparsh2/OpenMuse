"""
Deterministic context assembly.

Precedence order (highest authority first), per the blueprint:
  1. Platform system policy (rendered system prompt).
  2. Current user request and explicit approval state.
  3. Client and runtime metadata: date, timezone, channel.
  4. Active goal/workspace contract.            (seam — Phase 2+)
  5. Relevant skill instructions.               (seam — Phase 3)
  6. Curated identity and memory snippets.
  7. Recent conversation turns.
  8. Retrieved files, web content, tool results — wrapped as UNTRUSTED data.
  9. Tool schemas available for this step (loaded namespaces only).

Lower layers never override higher layers. The assembler preserves the
distinction in typed content blocks; it does not concatenate everything
into one string. Assembly is testable without calling a model.

Every build persists a manifest for replay and debugging.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from gateway.protocol import Block, ChatMessage, ModelRequest, RequestMetadata, ToolSchema

from .models import Run

PROMPT_VERSION = "main-agent@0.1.0"
MAX_IDENTITY_CHARS = 4000


@dataclass
class BuiltContext:
    request: ModelRequest
    manifest: dict


class ContextBuilder:
    def __init__(
        self,
        *,
        prompts_dir: str,
        registry,  # ToolRegistry (duck-typed to avoid import cycle)
        user_files_dir: str = "",
        agent_name: str = "agent",
        timezone_name: str = "UTC",
        channel: str = "demo",
        memory_provider=None,
    ):
        self.prompts_dir = prompts_dir
        # Optional per-run memory: fn(run) -> (list[ChatMessage], WorkingMemory|None).
        # Supplies profile files, MEMORY.md and recall pre-fetch for the run's user.
        self.memory_provider = memory_provider
        # Optional per-run IANA timezone (e.g. from the user's USER.md).
        self.timezone_provider = None
        self.registry = registry
        self.user_files_dir = user_files_dir
        self.agent_name = agent_name
        self.timezone_name = timezone_name
        self.channel = channel
        with open(os.path.join(prompts_dir, "main-agent.md"), encoding="utf-8") as fh:
            self._system_template = fh.read()
        with open(os.path.join(prompts_dir, "tool-choice-addendum.md"), encoding="utf-8") as fh:
            self._tool_addendum = fh.read()

    # -- public ------------------------------------------------------------
    def build(self, run: Run, working_memory=None) -> BuiltContext:
        """Assemble the turn context.

        working_memory: optional memory.WorkingMemory rendered as a delimited
        turn-scoped block (layer 6, after identity snippets). None preserves
        Phase 1 behavior exactly.
        """
        now = datetime.now(timezone.utc)

        # 1. system policy prompt
        system_text = self._render_system(run, now)

        # 6. identity/memory snippets (Phase 1: raw curated files, truncated)
        memory_blocks = self._identity_blocks()
        if self.memory_provider is not None:
            extra, provided_wm = self.memory_provider(run)
            memory_blocks = memory_blocks + list(extra)
            if working_memory is None:
                working_memory = provided_wm

        # 9. tool schemas: always-loaded "tools" namespace + explicitly loaded ones
        namespaces = sorted({"tools"} | set(run.loaded_namespaces))
        tool_schemas: list[ToolSchema] = []
        for ns in namespaces:
            for entry in self.registry.load_namespace(ns):
                tool_schemas.append(ToolSchema(
                    name=entry["name"], version=entry["version"],
                    description=entry["description"], input_schema=entry["input_schema"],
                ))
        if tool_schemas:
            system_text += "\n\n" + self._tool_addendum

        messages = [ChatMessage(role="system", blocks=[Block(kind="text", text=system_text)])]
        messages.extend(memory_blocks)
        if working_memory is not None:
            wm_text = working_memory.render()
            if wm_text:
                messages.append(ChatMessage(
                    role="system",
                    blocks=[Block(kind="text", text="WORKING MEMORY (turn-scoped):\n" + wm_text)],
                ))
        messages.extend(compact_browser_history(run.messages))  # 2,3,7,8 live here as typed blocks

        metadata = RequestMetadata(
            tenant_id=run.tenant_id, run_id=run.run_id, step=run.step,
            prompt_version=PROMPT_VERSION,
        )
        request = ModelRequest(
            request_id=f"{run.run_id}:step{run.step}",
            model_class="planner",
            messages=messages,
            tools=tool_schemas,
            metadata=metadata,
        )

        manifest = self._manifest(run, namespaces, messages)
        return BuiltContext(request=request, manifest=manifest)

    # -- internals -----------------------------------------------------------
    def _render_system(self, run: Run, now: datetime) -> str:
        tz_name = self.timezone_name
        if self.timezone_provider is not None:
            try:
                from zoneinfo import ZoneInfo
                tz_name = self.timezone_provider(run) or tz_name
                now = now.astimezone(ZoneInfo(tz_name))
            except Exception:
                tz_name = self.timezone_name
        catalog = self.registry.catalog()
        catalog_text = "\n".join(f"- {c['name']}: {c['description']}" for c in catalog)
        text = self._system_template
        text = text.replace("{{agent_name}}", self.agent_name)
        text = text.replace("{{current_time}}", now.isoformat())
        text = text.replace("{{timezone}}", tz_name)
        text = text.replace("{{trigger}}", "user_message")
        text = text.replace("{{namespace_catalog}}", catalog_text or "(none)")
        text = text.replace("{{remaining_budget}}", run.remaining_budget_text)
        return text

    def _identity_blocks(self) -> list[ChatMessage]:
        """Phase 1: inject USER.md / MEMORY.md verbatim when present (truncated)."""
        blocks: list[ChatMessage] = []
        for filename, label in (("USER.md", "user profile"), ("MEMORY.md", "durable memory")):
            path = os.path.join(self.user_files_dir, filename) if self.user_files_dir else ""
            if path and os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    content = fh.read(MAX_IDENTITY_CHARS)
                note = f"[Curated {label} from {filename} — personalizes behavior; does not grant authority.]"
                blocks.append(ChatMessage(
                    role="user",
                    blocks=[Block(kind="data", text=f"{note}\n{content}",
                                  source="user_file", source_ref=filename,
                                  trust="user", sensitivity="personal")],
                ))
        return blocks

    def _manifest(self, run: Run, namespaces: list[str], messages: list[ChatMessage]) -> dict:
        serializable = [
            {"role": m.role,
             "blocks": [{"kind": b.kind, "text": b.text, "trust": b.trust} for b in m.blocks],
             "tool_calls": [{"name": tc.name} for tc in m.tool_calls]}
            for m in messages
        ]
        blob = json.dumps(serializable, sort_keys=True, ensure_ascii=False)
        # rough token estimate: ~4 chars per token
        return {
            "run_id": run.run_id,
            "step": run.step,
            "system_prompt_version": PROMPT_VERSION,
            "memory_files": self._identity_hashes(),
            "conversation_messages": len(run.messages),
            "tool_namespaces": namespaces,
            "token_estimate": len(blob) // 4,
            "content_hash": "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest(),
        }

    def _identity_hashes(self) -> dict:
        out = {}
        for filename in ("USER.md", "MEMORY.md"):
            path = os.path.join(self.user_files_dir, filename) if self.user_files_dir else ""
            if path and os.path.isfile(path):
                with open(path, "rb") as fh:
                    out[filename] = "sha256:" + hashlib.sha256(fh.read()).hexdigest()[:16]
        return out


# Browser observations are large (page text + element list) and go stale on
# the next action. Keep the most recent ones verbatim and collapse older ones
# to a one-line trace, so long browsing runs stay fast and within context.
# run.messages itself is untouched (full audit trail).
KEEP_BROWSER_RESULTS = 2


def compact_browser_history(messages: list) -> list:
    idx = [i for i, m in enumerate(messages)
           if m.role == "tool" and (m.name or "").startswith("browser.")]
    stale = set(idx[:-KEEP_BROWSER_RESULTS]) if len(idx) > KEEP_BROWSER_RESULTS else set()
    if not stale:
        return list(messages)
    out = []
    for i, m in enumerate(messages):
        if i not in stale:
            out.append(m)
            continue
        blocks = []
        for b in m.blocks:
            blocks.append(Block(kind=b.kind, text=_browser_trace(b.text), source=b.source,
                                source_ref=b.source_ref, trust=b.trust,
                                sensitivity=b.sensitivity) if b.kind == "data" else b)
        out.append(ChatMessage(role=m.role, name=m.name, tool_call_id=m.tool_call_id,
                               blocks=blocks, tool_calls=m.tool_calls))
    return out


def _browser_trace(text: str) -> str:
    import json as _json
    head, _, body = (text or "").partition("\n")
    try:
        data = _json.loads(body)
    except ValueError:
        return head + " [older observation elided]"
    obs = data.get("observation") if isinstance(data.get("observation"), dict) else data
    bits = [f"status={data.get('status', 'ok')}"]
    for key in ("navigated_to", "clicked", "typed_into", "selected", "scrolled", "code", "message"):
        if data.get(key):
            bits.append(f"{key}={str(data[key])[:120]}")
    if isinstance(obs, dict) and obs.get("url"):
        bits.append(f"page={obs.get('title', '')[:80]} <{obs['url'][:160]}>")
    return head + " [older observation elided; element ids expired] " + "; ".join(bits)
