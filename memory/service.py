"""
MemoryService — per-user memory runtime.

Owns one LayeredMemory per user (users/<user_id>/memory) plus that user's
authored profile files (users/<user_id>/profile/{IDENTITY,SOUL,USER,AGENTS}.md),
and runs the blueprint's pipelines around each turn:

  before a turn  turn_context(): profile files + MEMORY.md projection as
                 personalization blocks, and a recall pre-fetch delivered as
                 turn-scoped WorkingMemory.
  after a turn   after_turn() (background): LLM extractor proposes candidates
                 from the USER's words; the LLM consolidator decides how they
                 relate to active memory; the RUNTIME validates and applies the
                 change set (add / reinforce / refine / supersede / journal /
                 people page). Then the chat's rolling summary is compacted.
  documents      ingest(): knowledge-bank chunks + passage embeddings.

The model never writes memory directly. Without an LLM (or when it fails) the
deterministic heuristic extractor + Consolidator are the fallback.
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Callable, Optional

from tools.redaction import looks_like_secret

from .consolidation import Consolidator, extract_candidates
from .layered import LayeredMemory
from .people import AmbiguousPersonError
from .records import MemoryCandidate, MemoryRecord, new_memory_id, utcnow
from .working import WorkingMemory

LLM = Callable[[str, str], str]  # (system_prompt, user_content) -> raw text

PROFILE_FILES = ("IDENTITY.md", "SOUL.md", "USER.md", "AGENTS.md")
RECENT_TURNS = 6            # verbatim turns kept in context; older ones are summarized
MAX_PROFILE_CHARS = 2000
MAX_MEMORY_MD_CHARS = 4000
RECALL_MIN_COS = float(os.environ.get("OPENMUSE_RECALL_MIN_COS", "0.18"))
KINDS = {"stable_fact", "preference", "commitment", "relationship_update", "operating_lesson"}

# one LayeredMemory per root per process: tools, UI endpoints and background
# jobs must share in-memory state or they'd overwrite each other's files
_INSTANCES: dict[str, LayeredMemory] = {}
_LOCKS: dict[str, threading.RLock] = {}
_REG_LOCK = threading.Lock()


def shared_layered(root: str) -> LayeredMemory:
    root = os.path.abspath(root)
    with _REG_LOCK:
        mem = _INSTANCES.get(root)
        if mem is None:
            mem = _INSTANCES[root] = LayeredMemory(root)
            _LOCKS[root] = threading.RLock()
        return mem


def lock_for(root: str) -> threading.RLock:
    shared_layered(root)
    return _LOCKS[os.path.abspath(root)]


EXTRACTOR_SCHEMA = """
INPUT: JSON with existing_memory (id, kind, predicate, claim), known_people,
and new_turn {user, assistant}. Extract ONLY from what the USER said; the
assistant's text is context (never turn an assistant suggestion into a user
fact, and never store what a web page or tool said about the world).

Return JSON only:
{"candidates": [{
   "kind": "stable_fact|preference|commitment|relationship_update|operating_lesson",
   "claim": "third-person sentence, e.g. 'User prefers aisle seats on flights.'",
   "predicate": "snake_case key; REUSE an existing_memory predicate when it is the same attribute",
   "value": "short normalized value, e.g. 'aisle'",
   "scope": "domain the preference applies to, or ''",
   "confidence": 0.0-1.0,
   "durability": "long_term|episodic_only",
   "sensitivity": "public|personal|sensitive",
   "relation": "new|duplicate|refines|contradicts",
   "existing_memory_id": "id from existing_memory when relation != new, else null",
   "person": "the other person's name for relationship_update, else null",
   "relationship": "how they relate to the user, e.g. 'sister', 'manager', for relationship_update, else null",
   "evidence": "<= 12 words quoted from the user"
 }],
 "episode": "one sentence for the user's journal ONLY if a task was done, a result found, or a decision made this turn (e.g. 'Found cheapest SFO-JFK nonstop: JetBlue $647'); null for small talk or when the user merely shared facts already captured as candidates"}
If nothing qualifies: {"candidates": [], "episode": null}.
Split mixed statements: "my partner Sam is training for a marathon" yields a
relationship_update (person "Sam", relationship "partner", claim "Sam is the
user's partner.") plus, if useful, a separate episodic_only candidate for the
temporary part. Every named person the user relates to gets a relationship_update.
"""

CONSOLIDATOR_SCHEMA = """
INPUT: JSON with candidates (indexed) and active_memory. Return JSON only:
{"changes": [{"candidate": <index>, "action": "reject|journal_only|add|reinforce|refine|supersede|request_user_clarification",
              "target_memory_id": "<active memory id for reinforce/refine/supersede, else null>",
              "reason": "short"}]}
One entry per candidate.
"""

COMPACTOR_SCHEMA = """
INPUT: JSON with current_summary (may be null) and new_turns [{seq, user, assistant}].
Merge them into ONE updated summary of the whole conversation so far. Return JSON only:
{"text": "2-4 sentence overview", "decisions": [], "commitments": [], "stable_facts": [],
 "open_threads": [], "safety_relevant": [], "artifacts": [], "omissions": []}
Each list item is one short string ending with its source turn(s), e.g. "... (turn 3)".
"""


def _load_prompt(prompts_dir: str, name: str) -> str:
    try:
        with open(os.path.join(prompts_dir, name), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _json(raw: str) -> Optional[dict]:
    """Parse the first JSON object in a model reply (tolerates prose/fences)."""
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else None
    except ValueError:
        pass
    start = raw.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(raw)):
            depth += {"{": 1, "}": -1}.get(raw[i], 0)
            if depth == 0:
                try:
                    out = json.loads(raw[start:i + 1])
                    return out if isinstance(out, dict) else None
                except ValueError:
                    break
        start = raw.find("{", start + 1)
    return None


def _snake(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")[:60] or "user_fact"


class MemoryService:
    def __init__(self, data_root: str, *, prompts_dir: str, llm: Optional[LLM] = None,
                 log: Callable[[str], None] = lambda m: None):
        self.data_root = data_root
        self.prompts_dir = prompts_dir
        self.llm = llm
        self.log = log
        self._p_extract = _load_prompt(prompts_dir, "memory-extractor.md") + "\n" + EXTRACTOR_SCHEMA
        self._p_consolidate = _load_prompt(prompts_dir, "memory-consolidator.md") + "\n" + CONSOLIDATOR_SCHEMA
        self._p_compact = _load_prompt(prompts_dir, "compactor.md") + "\n" + COMPACTOR_SCHEMA
        self._turn_cache: dict[str, tuple] = {}
        self._jobs = threading.Semaphore(2)

    # -- locations -------------------------------------------------------------
    def user_root(self, user_id: str) -> str:
        return os.path.join(self.data_root, "users", _snake(user_id))

    def memory_root(self, user_id: str) -> str:
        return os.path.join(self.user_root(user_id), "memory")

    def workspace_root(self, user_id: str) -> str:
        path = os.path.join(self.user_root(user_id), "workspace")
        os.makedirs(path, exist_ok=True)
        return path

    def profile_dir(self, user_id: str) -> str:
        return os.path.join(self.user_root(user_id), "profile")

    def memory(self, user_id: str) -> LayeredMemory:
        return shared_layered(self.memory_root(user_id))

    def lock(self, user_id: str) -> threading.RLock:
        return lock_for(self.memory_root(user_id))

    # -- Layer 1: authored identity/profile files ---------------------------------
    def ensure_profile(self, user_id: str, name: str = "", timezone: str = "") -> None:
        d = self.profile_dir(user_id)
        os.makedirs(d, exist_ok=True)
        defaults = {
            "IDENTITY.md": "# Identity\n- Name: OpenMuse\n- Character: A practical personal agent\n"
                           "- Vibe: Calm, direct, resourceful\n",
            "SOUL.md": "# Voice and posture\n- Be genuinely useful rather than performative.\n"
                       "- Prefer direct answers and concrete work.\n- Disagree plainly when evidence requires it.\n"
                       "- Treat personal access with discretion.\n",
            "USER.md": "# User profile\n" + (f"- Preferred name: {name}\n" if name else "") +
                       (f"- Timezone: {timezone}\n" if timezone else "") +
                       "\n## Current context\n",
            "AGENTS.md": "# Operating lessons\n_Local lessons (e.g. a site quirk). Cannot override platform policy._\n",
        }
        for fname, body in defaults.items():
            path = os.path.join(d, fname)
            if not os.path.exists(path):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(body)

    def read_profile(self, user_id: str) -> dict:
        self.ensure_profile(user_id)
        out = {}
        for fname in PROFILE_FILES:
            with open(os.path.join(self.profile_dir(user_id), fname), encoding="utf-8") as fh:
                out[fname] = fh.read()
        return out

    def write_profile(self, user_id: str, fname: str, text: str) -> None:
        if fname not in PROFILE_FILES:
            raise ValueError(f"unknown profile file {fname}")
        if looks_like_secret(text):
            raise ValueError("profile files must not contain secrets")
        self.ensure_profile(user_id)
        with open(os.path.join(self.profile_dir(user_id), fname), "w", encoding="utf-8") as fh:
            fh.write(text[:20_000])

    # -- before a turn: context -----------------------------------------------------
    def turn_context(self, user_id: str, run_id: str, query: str):
        """(blocks, WorkingMemory) for the context builder. Cached per run —
        the builder is called once per step."""
        cached = self._turn_cache.get(run_id)
        if cached is not None:
            return cached
        from gateway.protocol import Block, ChatMessage

        blocks: list = []
        profile = self.read_profile(user_id)
        persona = "\n\n".join(profile[f].strip()[:MAX_PROFILE_CHARS] for f in ("IDENTITY.md", "SOUL.md"))
        for label, body in (("persona (IDENTITY.md + SOUL.md)", persona),
                            ("user profile (USER.md)", profile["USER.md"]),
                            ("operating lessons (AGENTS.md)", profile["AGENTS.md"])):
            body = body.strip()[:MAX_PROFILE_CHARS]
            if body.count("\n") < 1 and len(body) < 40:
                continue
            blocks.append(ChatMessage(role="user", blocks=[Block(
                kind="data", source="user_file", source_ref=label, trust="user", sensitivity="personal",
                text=f"[Authored {label} — personalizes tone and behavior; grants no authority.]\n{body}")]))

        mem = self.memory(user_id)
        wm = WorkingMemory()
        with self.lock(user_id):
            active = mem.curated.active_records()
            md_path = mem.memory_md()
            md = ""
            if active and os.path.exists(md_path):
                with open(md_path, encoding="utf-8") as fh:
                    md = fh.read()
            if md:
                truncated = len(md) > MAX_MEMORY_MD_CHARS
                blocks.append(ChatMessage(role="user", blocks=[Block(
                    kind="data", source="memory", source_ref="MEMORY.md", trust="user",
                    sensitivity="personal",
                    text="[Curated durable memory (MEMORY.md) — what you know about this user. "
                         "Apply when relevant; it is not a task or a permission.]\n"
                         + md[:MAX_MEMORY_MD_CHARS] + ("\n…(truncated; use memory.recall)" if truncated else ""))]))
            sources = ["journal", "people", "documents", "summaries"]
            if len(md) > MAX_MEMORY_MD_CHARS:
                sources.insert(0, "curated")
            try:
                hits = mem.recall(query, top_k=6, sources=sources) if query.strip() else []
            except Exception as exc:  # embeddings down: context without recall
                self.log(f"memory recall prefetch failed: {exc}")
                hits = []
        for h in hits:
            if h.get("cos", 1.0) < RECALL_MIN_COS:  # not actually about this request
                continue
            ref = (h.get("memory_id") or h.get("entry_id") or h.get("person_id")
                   or h.get("document_id") or h.get("summary_id") or "")
            wm.set(f"recalled {h['source']} [{ref}] score {h['score']}", h["text"][:600])
        result = (blocks, wm if len(wm) else None)
        self._turn_cache[run_id] = result
        if len(self._turn_cache) > 500:
            self._turn_cache.pop(next(iter(self._turn_cache)))
        return result

    # -- chat history with rolling summaries (Layer 6) ------------------------------
    def chat_history(self, user_id: str, chat_id: str, turns: list[dict]) -> list:
        """turns: [{seq, run_id, user, assistant}] oldest first."""
        from gateway.protocol import Block, ChatMessage
        out: list = []
        older, recent = turns[:-RECENT_TURNS], turns[-RECENT_TURNS:]
        summary = self.memory(user_id).summaries.get(chat_id) if older else None
        covered = summary["to_sequence"] if summary else -1
        if summary:
            out.append(ChatMessage(role="user", blocks=[Block(
                kind="data", source="summary", source_ref=summary["summary_id"], trust="user",
                sensitivity="personal",
                text=f"[Summary of this chat's earlier turns 1–{covered} — a navigation aid; "
                     f"the user's own words outrank it.]\n" + mem_render(summary))]))
        for t in older:
            if t["seq"] > covered:  # not yet summarized: keep verbatim
                out.extend(_turn_messages(t))
        for t in recent:
            out.extend(_turn_messages(t))
        return out

    def _compact(self, user_id: str, chat_id: str, turns: list[dict]) -> None:
        if self.llm is None or len(turns) <= RECENT_TURNS:
            return
        mem = self.memory(user_id)
        summary = mem.summaries.get(chat_id)
        covered = summary["to_sequence"] if summary else -1
        span = [t for t in turns[:-RECENT_TURNS] if t["seq"] > covered]
        if not span:
            return
        payload = {"current_summary": summary and {k: summary.get(k) for k in (
                       "text", "decisions", "commitments", "stable_facts", "open_threads",
                       "safety_relevant", "artifacts")},
                   "new_turns": [{"seq": t["seq"], "user": t["user"][:2000],
                                  "assistant": t["assistant"][:2500]} for t in span]}
        data = _json(self.llm(self._p_compact, json.dumps(payload, ensure_ascii=False)))
        if not data:
            self.log("compactor returned no JSON; summary unchanged")
            return
        data["from_sequence"] = summary["from_sequence"] if summary else span[0]["seq"]
        data["to_sequence"] = span[-1]["seq"]
        refs = (summary or {}).get("source_refs", []) + [f"run:{t['run_id']}" for t in span]
        with self.lock(user_id):
            mem.summaries.put(chat_id, data, refs)
        self.log(f"summarized chat {chat_id} turns ≤{data['to_sequence']}")

    # -- after a turn: extraction + consolidation --------------------------------------
    def after_turn(self, user_id: str, chat_id: str, run_id: str, user_text: str,
                   assistant_text: str, turns: list[dict], *, already_noted: bool = False) -> None:
        """Fire-and-forget: memory writes never slow the conversational turn.
        already_noted: the agent stored this turn via memory.note (which runs
        the same pipeline), so don't extract it twice."""
        def job():
            with self._jobs:
                try:
                    if not already_noted:
                        self.learn(user_id, user_text, assistant_text, source_ref=f"run:{run_id}")
                except Exception as exc:
                    self.log(f"memory extraction failed: {exc}")
                try:
                    self._compact(user_id, chat_id, turns)
                except Exception as exc:
                    self.log(f"compaction failed: {exc}")
        threading.Thread(target=job, daemon=True, name=f"memory-{run_id}").start()

    def learn(self, user_id: str, user_text: str, assistant_text: str = "",
              source_ref: str = "") -> dict:
        return self.learn_at(self.memory_root(user_id), user_text, assistant_text, source_ref)

    def learn_at(self, root: str, user_text: str, assistant_text: str = "",
                 source_ref: str = "") -> dict:
        """Extract -> consolidate -> apply, for one user's memory root."""
        mem = shared_layered(root)
        if self.llm is None:
            return self._heuristic(mem, root, user_text, source_ref,
                                   always_journal=source_ref.startswith("memory.note"))
        with lock_for(root):
            existing = mem.curated.active_records()
            related = existing
            if len(existing) > 40:
                qv = mem.embedder.embed_query(user_text)
                related = [mem.curated.get(h["memory_id"])
                           for h in mem.curated.recall(user_text, qv, top_k=25)]
                related = [r for r in related if r is not None]
            people = [p["name"] for p in mem.people.list_people()]
        payload = {"existing_memory": [{"id": r.memory_id, "kind": r.kind, "predicate": r.predicate,
                                        "claim": r.claim} for r in related],
                   "known_people": people,
                   "new_turn": {"user": user_text[:4000], "assistant": assistant_text[:1500]}}
        data = _json(self.llm(self._p_extract, json.dumps(payload, ensure_ascii=False)))
        if data is None:
            self.log("extractor returned no JSON; using heuristic fallback")
            return self._heuristic(mem, root, user_text, source_ref,
                                   always_journal=source_ref.startswith("memory.note"))
        cands = [c for c in (data.get("candidates") or []) if isinstance(c, dict)][:12]
        applied: list[dict] = []
        actions = self._consolidate(mem, cands, related) if any(
            c.get("relation") not in (None, "new") for c in cands) else {}
        with lock_for(root):
            for i, c in enumerate(cands):
                applied.append(self._apply(mem, c, actions.get(i), source_ref))
            episode = data.get("episode")
            if isinstance(episode, str) and len(episode.strip()) > 8 and not looks_like_secret(episode):
                entry = mem.journal.write_entry(title="Conversation", text=episode.strip()[:600],
                                                event_refs=[source_ref] if source_ref else [])
                mem._index_journal_entry(entry, entry.text)
                mem.derivation.link("message", source_ref or entry.entry_id, "journal",
                                    entry.entry_id, "extracted_from")
                applied.append({"op": "journal", "id": entry.entry_id})
        if applied:
            self.log(f"memory: {[a['op'] for a in applied]}")
        return {"ops": applied}

    def _consolidate(self, mem: LayeredMemory, cands: list[dict], active: list) -> dict:
        payload = {"candidates": [{"index": i, **{k: c.get(k) for k in (
                        "kind", "claim", "predicate", "value", "relation", "existing_memory_id")}}
                                  for i, c in enumerate(cands)],
                   "active_memory": [{"id": r.memory_id, "kind": r.kind, "predicate": r.predicate,
                                      "claim": r.claim, "created_at": r.created_at} for r in active]}
        data = _json(self.llm(self._p_consolidate, json.dumps(payload, ensure_ascii=False))) or {}
        out = {}
        for ch in data.get("changes") or []:
            try:
                out[int(ch.get("candidate"))] = ch
            except (TypeError, ValueError):
                continue
        return out

    def _apply(self, mem: LayeredMemory, c: dict, change: Optional[dict], source_ref: str) -> dict:
        kind = c.get("kind")
        claim = str(c.get("claim") or "").strip()[:300]
        if kind not in KINDS or not claim or looks_like_secret(claim) or looks_like_secret(str(c.get("value", ""))):
            return {"op": "reject", "why": "invalid or secret-bearing"}
        refs = [source_ref] if source_ref else []
        action = (change or {}).get("action") or {
            "duplicate": "reinforce", "refines": "refine", "contradicts": "supersede",
        }.get(c.get("relation"), "add")
        if c.get("durability") == "episodic_only" and action == "add":
            action = "journal_only"
        target = mem.curated.get((change or {}).get("target_memory_id") or c.get("existing_memory_id") or "")
        if action in ("reinforce", "refine", "supersede") and (target is None or target.status != "active"):
            action = "add"  # the model named no valid target: runtime falls back to rule-based upsert
        if action == "refine" and target is not None:
            # blueprint rule, enforced by the runtime: a refinement extends the
            # old value; a different value is a contradiction -> supersede
            old_v = str(target.value.get("value", "")).strip().lower()
            new_v = str(c.get("value") or "").strip().lower()
            if old_v and new_v and not (new_v.startswith(old_v) or old_v in new_v):
                action = "supersede"

        if action in ("reject", "request_user_clarification"):
            return {"op": action, "claim": claim}
        # people pages hold both lasting facts and recent context about a person
        if kind == "relationship_update" and c.get("person"):
            try:
                person = mem.add_person_fact(str(c["person"])[:80], claim, source_ref or "",
                                             relationship=str(c.get("relationship") or "")[:80])
                return {"op": "person", "id": person["person_id"], "name": person["name"]}
            except AmbiguousPersonError as exc:
                entry = mem.journal.write_entry(
                    title="Needs clarification", text=f"{claim} (which {exc.name}?)",
                    event_refs=refs, tags=["needs_clarification"])
                mem._index_journal_entry(entry, entry.text)
                return {"op": "request_user_clarification", "id": entry.entry_id}

        if action == "journal_only":
            entry = mem.journal.write_entry(title="Noted", text=claim, event_refs=refs)
            mem._index_journal_entry(entry, claim)
            return {"op": "journal_only", "id": entry.entry_id}
        conf = max(0.3, min(0.99, float(c.get("confidence") or 0.8)))
        value = {"value": str(c.get("value") or claim)[:200]}
        if action == "reinforce":
            for r in refs:
                if r not in target.source_refs:
                    target.source_refs.append(r)
            target.confidence = min(0.99, target.confidence + 0.02)
            mem.curated.update(target)
            return {"op": "reinforce", "id": target.memory_id}
        if action == "refine":
            target.history.append({"value": dict(target.value), "claim": target.claim, "at": utcnow()})
            target.value, target.claim = value, claim
            target.source_refs.extend(r for r in refs if r not in target.source_refs)
            mem.curated.update(target)
            mem._index_record(target)
            return {"op": "refine", "id": target.memory_id}
        if action == "supersede":
            rec = MemoryRecord(memory_id=new_memory_id(), kind=kind, predicate=target.predicate,
                               claim=claim, value=value, scope=str(c.get("scope") or target.scope),
                               confidence=conf, sensitivity=c.get("sensitivity") or "personal",
                               source_refs=refs, supersedes=target.memory_id,
                               created_by="memory_consolidator@llm")
            mem.curated.add(rec)
            target.status, target.superseded_by = "superseded", rec.memory_id
            mem.curated.update(target)
            mem._index_record(rec)
            mem.vectors.remove([f"memory:{target.memory_id}"])
            mem.derivation.link("message", source_ref or rec.memory_id, "memory", rec.memory_id,
                                "consolidated_into")
            return {"op": "supersede", "id": rec.memory_id, "old": target.memory_id}
        # add (rule-based safety net still catches same-predicate dup/contradiction)
        cand = MemoryCandidate(kind=kind, claim=claim, predicate=_snake(c.get("predicate") or ""),
                               normalized=value, scope=str(c.get("scope") or ""), source_refs=refs,
                               confidence=conf, sensitivity=c.get("sensitivity") or "personal")
        op = Consolidator(mem.curated)._upsert(cand, "memory_consolidator@llm")
        rec = mem.curated.get(op.memory_id or "")
        if rec is not None:
            mem._index_record(rec)
            mem.derivation.link("message", source_ref or rec.memory_id, "memory", rec.memory_id,
                                "consolidated_into")
        return {"op": op.op, "id": op.memory_id}

    def _heuristic(self, mem: LayeredMemory, root: str, text: str, source_ref: str,
                   always_journal: bool = False) -> dict:
        """Fallback pipeline. An explicit memory.note is always journaled (the
        user/agent asked to remember it); a plain turn only when a durable
        statement pattern matches, so small talk never lands in memory."""
        cands = extract_candidates(text, source_ref=source_ref)
        if not cands and not always_journal:
            return {"ops": []}
        with lock_for(root):
            result = mem.remember(text, source_ref=source_ref)
        return {"ops": result["ops"]}

    # -- knowledge bank (Layer 5) ---------------------------------------------------------
    def ingest(self, user_id: str, title: str, text: str = "", data: bytes = b"",
               filename: str = "") -> dict:
        if data and filename.lower().endswith(".pdf"):
            text = _pdf_text(data)
        elif data:
            text = data.decode("utf-8", errors="replace")
        if looks_like_secret(text[:20000]):
            raise ValueError("this document looks like it contains secrets; not indexed")
        mem = self.memory(user_id)
        with self.lock(user_id):
            return mem.documents.add(title or filename or "Untitled", text)


def mem_render(summary: dict) -> str:
    from .knowledge import SummaryStore
    return SummaryStore.render(summary)


def _turn_messages(t: dict) -> list:
    from gateway.protocol import Block, ChatMessage
    out = [ChatMessage(role="user", blocks=[Block(kind="text", text=t["user"], trust="user")])]
    if t.get("assistant"):
        out.append(ChatMessage(role="assistant", blocks=[Block(kind="text", text=t["assistant"])]))
    return out


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("PDF support needs the pypdf package") from exc
    import io
    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)
