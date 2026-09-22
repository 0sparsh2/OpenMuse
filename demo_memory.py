#!/usr/bin/env python3
"""
Phase 2 verification: layered memory end to end.

Proves:
  1. remember() -> journal + curated records + vectors + derivation edges.
  2. Semantic/hybrid recall returns the right records (direct + paraphrase).
  3. A contradictory preference SUPERSEDES the old one (no duplication;
     old record excluded from default recall, visible with history).
  4. forget() removes a fact: record tombstoned, vectors gone, MEMORY.md
     regenerated without it, audit marker written, recall no longer
     surfaces it (lexical or semantic).
  5. Ambiguous forget targets are never deleted; unknown targets -> not_found.
  6. memory.note / memory.recall / memory.forget tools work through the
     registry with the Phase 2 policy mapping (R2/R1/R2).
  7. Working memory renders as a turn-scoped context block.

Run:  python3 demo_memory.py
Exit code 0 = all checks passed.
"""
from __future__ import annotations

import os
import sys
import tempfile
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from memory import LayeredMemory, WorkingMemory  # noqa: E402
from tools import ToolRegistry  # noqa: E402
from tools.namespaces import memory_tools  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = ""):
    CHECKS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="openmuse-mem2-")
    mem = LayeredMemory(os.path.join(tmp, ".agent-memory"))

    # -- 1. write facts -------------------------------------------------------
    r1 = mem.remember("I prefer dinner reservations at 7 PM or later.", source_ref="msg_1")
    r2 = mem.remember("My favorite cocktail is the espresso martini.", source_ref="msg_2")
    r3 = mem.remember("My job is Data Scientist at Boeing.", source_ref="msg_3")
    check("remember extracts durable records",
          len(r1["memory_ids"]) == 1 and len(r2["memory_ids"]) == 1 and len(r3["memory_ids"]) == 1,
          f"{r1['memory_ids']}, {r2['memory_ids']}, {r3['memory_ids']}")
    check("journal entries written", len(mem.journal.recent_entries(10)) == 3)
    check("vectors indexed", len(mem.vectors) >= 3, f"{len(mem.vectors)} vectors")
    check("derivation edges recorded",
          len(mem.derivation.derivatives_of("journal", r1["journal_entry_id"])) > 0)
    check("MEMORY.md projection exists and cites records",
          os.path.exists(mem.memory_md()) and r1["memory_ids"][0] in open(mem.memory_md()).read())

    dinner_id = r1["memory_ids"][0]
    cocktail_id = r2["memory_ids"][0]
    job_id = r3["memory_ids"][0]

    # -- 2. recall --------------------------------------------------------------
    hits = mem.recall("dinner reservation preference", top_k=3)
    check("recall: dinner preference top-1",
          hits and hits[0]["memory_id"] == dinner_id,
          f"top={[h.get('memory_id') for h in hits]}")
    hits = mem.recall("favorite cocktail", top_k=3)
    check("recall: cocktail top-1",
          hits and hits[0]["memory_id"] == cocktail_id)
    hits = mem.recall("user's job", top_k=3)
    check("recall: job top-1",
          hits and hits[0]["memory_id"] == job_id)
    hits = mem.recall("evening meal timing preference", top_k=3)
    check("recall: paraphrase finds dinner preference in top-3",
          any(h.get("memory_id") == dinner_id for h in hits),
          f"top={[h.get('memory_id') for h in hits]}")
    check("recall hits carry why-tags", all(h.get("why") for h in
                                            mem.recall("dinner", top_k=2)))

    # people layer
    mem.add_person_fact("Rohan Mehta", "Rohan Mehta is the user's brother.", source_ref="msg_4")
    mem.add_person_fact("Priya Iyer", "Priya Iyer is a college friend.", source_ref="msg_5")
    hits = mem.recall("user's brother", top_k=5, sources=["people"])
    check("recall: people source finds brother",
          any("Rohan Mehta" in h["text"] for h in hits),
          f"{[h['text'] for h in hits]}")

    # -- 3. contradiction -> supersede -------------------------------------------
    r4 = mem.remember("I prefer dinner reservations at 6 PM.", source_ref="msg_6")
    new_dinner_id = r4["memory_ids"][0]
    ops = [o["op"] for o in r4["ops"]]
    check("contradiction supersedes (not duplicates)", "supersede" in ops, f"ops={ops}")
    old = mem.curated.get(dinner_id)
    check("old record marked superseded", old is not None and old.status == "superseded")
    hits = mem.recall("dinner preference", top_k=5)
    ids = [h.get("memory_id") for h in hits]
    check("new preference wins default recall",
          ids and ids[0] == new_dinner_id, f"top={ids[:3]}")
    check("superseded record excluded from default recall", dinner_id not in ids)
    hits_hist = mem.recall("dinner preference", top_k=10, include_history=True)
    check("history recall surfaces superseded record",
          dinner_id in [h.get("memory_id") for h in hits_hist])

    # -- 4. forget ---------------------------------------------------------------
    res = mem.forget("espresso martini")
    check("forget completes", res.status == "completed", res.note)
    check("forget verified", res.verified)
    rec = mem.curated.get(cocktail_id)
    check("record tombstoned, content dropped",
          rec is not None and rec.status == "tombstoned" and rec.value == {})
    proj = open(mem.memory_md()).read()
    check("MEMORY.md regenerated without the forgotten fact",
          "espresso" not in proj.lower() and cocktail_id not in proj)
    hits = mem.recall("favorite cocktail", top_k=5)
    check("lexical+semantic recall no longer surfaces it",
          not any(h.get("memory_id") == cocktail_id or "espresso" in h["text"].lower()
                   for h in hits),
          f"{[h['text'][:40] for h in hits]}")
    audit = os.path.join(mem.root, "forget-audit.log")
    check("non-content audit marker written",
          os.path.exists(audit) and "user-directed deletion completed" in open(audit).read()
          and "espresso" not in open(audit).read().lower())

    # -- 5. ambiguous + not_found --------------------------------------------------
    # A second Rohan: ambiguity is surfaced, then resolved explicitly by the
    # caller ("yes, a different person") — never auto-merged.
    try:
        mem.add_person_fact("Rohan Verma", "Rohan Verma is a former colleague.", source_ref="msg_7")
        check("second Rohan raises ambiguity (no silent merge)", False)
    except Exception as e:
        check("second Rohan raises ambiguity (no silent merge)",
              "ambiguous" in str(type(e).__name__).lower() or "Ambiguous" in str(e),
              f"{type(e).__name__}: {e}")
    rv = mem.people.create_person("Rohan Verma", "former colleague")
    mem.people.add_fact("Rohan Verma", "Rohan Verma is a former colleague.", source_ref="msg_7")
    amb = mem.forget("Rohan")
    check("ambiguous forget never deletes",
          amb.status == "ambiguous" and not amb.removed,
          f"status={amb.status}")
    check("ambiguous targets preserved",
          len(mem.people.list_people()) == 3)
    nf = mem.forget("xyzzy nonexistent memory")
    check("unknown target -> not_found", nf.status == "not_found")

    # -- 6. tools through the registry -------------------------------------------
    registry = ToolRegistry()
    memory_tools.register(registry)
    ctx = SimpleNamespace(workspace_root=tmp)
    out = registry.get("memory.note").execute(ctx, {"note": "I prefer aisle seats on flights."})
    check("memory.note tool stores", out["stored"] and len(out["memory_ids"]) == 1, str(out))
    out = registry.get("memory.recall").execute(ctx, {"query": "flight seat preference"})
    check("memory.recall tool finds it",
          any("aisle" in h["text"].lower() for h in out["results"]))
    out = registry.get("memory.forget").execute(ctx, {"query": "aisle seats"})
    check("memory.forget tool completes+verifies",
          out["status"] == "completed" and out["verified"], str(out.get("note")))
    out = registry.get("memory.recall").execute(ctx, {"query": "flight seat preference"})
    check("post-forget tool recall clean",
          not any("aisle" in h["text"].lower() for h in out["results"]))

    import yaml  # noqa: E402
    with open(os.path.join(ROOT, "policies", "tool-capabilities.yaml")) as fh:
        caps = yaml.safe_load(fh)["tools"]
    check("policy: memory.recall is R1/private-read",
          caps["memory.recall"]["risk"] == "R1" and caps["memory.recall"]["side_effect"] == "none")
    check("policy: memory.note is R2/local-write",
          caps["memory.note"]["risk"] == "R2")
    check("policy: memory.forget is R2/local-write",
          caps["memory.forget"]["risk"] == "R2"
          and caps["memory.forget"]["side_effect"] == "local_write")

    # -- 7. working memory ----------------------------------------------------------
    wm = WorkingMemory()
    wm.set("open_question", "user has not chosen a cloud provider yet")
    wm.append("candidates", "aws")
    rendered = wm.render()
    check("working memory renders delimited block",
          "AGENT WORKING NOTES" in rendered and "cloud provider" in rendered)
    check("working memory is turn-scoped (clearable)",
          (wm.clear() or True) and len(wm) == 0 and wm.render() == "")

    # -- summary ----------------------------------------------------------------------
    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    for n, ok, detail in CHECKS:
        if not ok:
            print(f"  FAILED: {n} {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
