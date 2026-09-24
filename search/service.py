"""
Web search orchestrator (ChatGPT-style search for OpenMuse).

  queries (1-4, from the model) -> engines in parallel -> rank fusion + dedupe
  -> pick pages to open (Jev scores results; embeddings / engine order as
  fallback) -> fetch with the network guard -> readable text -> passages
  ranked with NIM embeddings -> numbered sources [n] (stable per run, so
  several searches in one answer share one numbering) -> "is this enough?"
  (Jev) so the model knows whether to search again.

After the answer is written, verify_citations() drops [n] markers the cited
source doesn't support (similarity + number checks).
"""
from __future__ import annotations

import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from search import netguard
from search.engines import DuckDuckGo, Result, fuse, normalize_url
from search.extract import chunks, extract

DEPTH = {  # pages to open, passage budget (chars), whether to run the sufficiency check
    "quick": {"open": 2, "budget": 4500, "check": False},
    "normal": {"open": 4, "budget": 8500, "check": True},
    "research": {"open": 7, "budget": 15000, "check": True},
}
LEVELS = ["Useless", "Off-topic", "Somewhat useful", "Very useful"]
_STOP = set("a an the of to in on for and or is are was were be been by with at from as it its this that these "
            "those what which who how when where why do does did can could will would should may might about into "
            "than then there their they you your we our not no yes if so but".split())


def _words(t: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9'\-]{1,}", (t or "").lower()) if w not in _STOP}


def _numbers(t: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", t or "")}


def _cos(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


class WebSearch:
    def __init__(self, *, engines=None, jev=None, embedder=None, fetch=None, clock=time.time):
        self.engines = engines if engines is not None else [DuckDuckGo()]
        self._jev = jev
        self._embedder = embedder
        self.fetch = fetch or netguard.safe_get
        self.clock = clock
        self._runs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="websearch")

    # lazily resolved so offline tests / no-key setups work
    @property
    def jev(self):
        if self._jev is None:
            from search.jev import default_jev
            self._jev = default_jev()
        return self._jev

    @property
    def embedder(self):
        if self._embedder is None:
            from memory.embeddings import default_embedder
            self._embedder = default_embedder()
        return self._embedder

    # -- per-run source numbering ---------------------------------------------------------
    def _state(self, run_id: str) -> dict:
        with self._lock:
            now = self.clock()
            for rid in [r for r, s in self._runs.items() if now - s["t"] > 6 * 3600]:
                self._runs.pop(rid, None)
            st = self._runs.setdefault(run_id or "_", {"t": now, "sources": {}, "order": [], "vectors": {}})
            st["t"] = now
            return st

    def _number(self, st: dict, url: str, title: str, date: str) -> int:
        key = normalize_url(url)
        with self._lock:
            src = st["sources"].get(key)
            if src is None:
                src = {"n": len(st["order"]) + 1, "url": url, "title": title, "date": date, "passages": []}
                st["sources"][key] = src
                st["order"].append(key)
            src["title"] = src["title"] or title
            src["date"] = src["date"] or date
            return src["n"]

    def sources(self, run_id: str) -> list[dict]:
        st = self._runs.get(run_id)
        if not st:
            return []
        return [st["sources"][k] for k in st["order"]]

    # -- search ----------------------------------------------------------------------------
    def search(self, run_id: str, queries: list[str], *, question: str = "", recency_days: int | None = None,
               depth: str = "normal", region: str = "wt-wt") -> dict:
        t0 = self.clock()
        cfg = DEPTH.get(depth, DEPTH["normal"])
        queries = [q.strip() for q in dict.fromkeys(queries) if q and q.strip()][:4]
        if not queries:
            raise ValueError("give 1-4 search queries")
        question = (question or "").strip() or queries[0]
        st = self._state(run_id)

        # 1. engines x queries, in parallel; 2. fuse
        jobs = {(e.name, q): self._pool.submit(e.search, q, recency_days=recency_days, region=region, max_results=8)
                for e in self.engines for q in queries}
        lists = {}
        for (ename, q), f in jobs.items():
            try:
                lists[f"{ename}:{q}"] = f.result(timeout=15)
            except Exception:
                lists[f"{ename}:{q}"] = []
        candidates = fuse(lists)[:20]
        if not candidates:
            return {"queries": queries, "sources": [], "found": 0, "read": 0, "enough": 0.0,
                    "note": "No results. Try different, simpler keywords, drop the date filter, or search in English."}

        # 3. which results matter (Jev -> embeddings -> engine order)
        scores, how = self._score_results(question, candidates)
        ranked = sorted(range(len(candidates)), key=lambda i: (-scores[i], i))
        keep = [i for i in ranked if scores[i] >= 1.0] or ranked[:5]      # drop off-topic results entirely
        to_open, per_site = [], {}
        for i in keep:
            site = candidates[i].site
            if per_site.get(site, 0) >= 2:
                continue
            per_site[site] = per_site.get(site, 0) + 1
            to_open.append(i)
            if len(to_open) >= cfg["open"]:
                break

        # 4. fetch + extract in parallel (failures are skipped, not waited on)
        pages = {}
        futs = {i: self._pool.submit(self._read, candidates[i].url) for i in to_open}
        for i, f in futs.items():
            try:
                page = f.result(timeout=12)
            except Exception:
                page = None
            if page and page["text"]:
                pages[i] = page

        # 5. passages: page chunks + snippets of relevant results we didn't open
        items = []   # (candidate index, text)
        for i, page in pages.items():
            for c in chunks(page["text"])[:25]:
                items.append((i, c))
        for i in keep[:12]:
            if i not in pages and candidates[i].snippet:
                items.append((i, candidates[i].snippet))
        ranked_items = self._rank_passages(st, [question] + queries, items)

        # 6. budgeted selection, at least one passage per opened page when it's relevant
        budget, used, chosen, taken, covered = cfg["budget"], 0, [], set(), set()
        for first_pass in (True, False):   # first: the best passage of each source; then fill by rank
            for pos, (score, (i, text)) in enumerate(ranked_items):
                if pos in taken or (first_pass and i in covered) or used + len(text) > budget:
                    continue
                chosen.append((i, text, score))
                taken.add(pos)
                covered.add(i)
                used += len(text)
        by_src: dict[int, list[str]] = {}
        for i, text, _ in sorted(chosen, key=lambda c: -c[2]):
            by_src.setdefault(i, []).append(text)

        out_sources = []
        for i in sorted(by_src, key=lambda i: ranked.index(i)):
            r = candidates[i]
            page = pages.get(i, {})
            n = self._number(st, r.url, page.get("title") or r.title, page.get("date") or r.date)
            src = st["sources"][normalize_url(r.url)]
            for p in by_src[i]:
                if p not in src["passages"]:
                    src["passages"].append(p)
            out_sources.append({"n": n, "title": (page.get("title") or r.title)[:160], "url": r.url, "site": r.site,
                                "date": page.get("date") or r.date, "opened": i in pages,
                                "passages": by_src[i]})

        # 7. is this enough to answer?
        enough = None
        if cfg["check"] and out_sources and self.jev.available:
            enough = self.jev.noul(
                {"question": question[:1000],
                 "passages": "\n\n".join(f"[{s['n']}] " + " ".join(s["passages"])[:1500] for s in out_sources)[:9000]},
                "The passages contain enough reliable information to answer the question fully and accurately")
        note = ("Cite sources inline as [n] right after the claims they support." if enough is None or enough >= 0.35
                else "These results look thin for this question: search again once with different or more specific "
                     "queries (or a different recency), then answer with what you found and say what's uncertain.")
        return {"queries": queries, "question": question, "sources": out_sources, "found": len(candidates),
                "read": len(pages), "ranked_by": how,
                "enough": None if enough is None else round(enough, 2), "ms": int((self.clock() - t0) * 1000),
                "note": note}

    def _read(self, url: str) -> dict | None:
        final, status, ctype, body = self.fetch(url, timeout=5.0)
        if status >= 400 or not body:
            return None
        page = extract(body, ctype, final)
        page["url"] = final
        return page

    def _score_results(self, question: str, cands: list[Result]) -> tuple[list[float], str]:
        state = {"question": question[:800]}
        for i, r in enumerate(cands):
            state[f"r{i + 1}"] = f"{r.title} — {r.site}{' — ' + r.date if r.date else ''}\n{r.snippet[:300]}"
        if self.jev.available:
            got = self.jev.scores(state, {f"r{i + 1}": f"How useful result r{i + 1} is for answering the question"
                                          for i in range(len(cands))}, LEVELS)
            if got:
                return [got.get(f"r{i + 1}", 0.0) for i in range(len(cands))], "jev"
        try:  # fallback: embedding similarity mapped onto the same 0-3 scale
            qv = self.embedder.embed_query(question)
            vs = self.embedder.embed([f"{r.title}. {r.snippet}" for r in cands])
            sims = [_cos(qv, v) for v in vs]
            lo, hi = min(sims), max(sims)
            return [1.0 + 2.0 * ((s - lo) / (hi - lo) if hi > lo else 1.0) for s in sims], "embeddings"
        except Exception:
            return [3.0 - 2.0 * i / max(1, len(cands)) for i in range(len(cands))], "engine order"

    def _rank_passages(self, st: dict, questions: list[str], items: list[tuple[int, str]]):
        if not items:
            return []
        try:
            qvs = [self.embedder.embed_query(q) for q in questions[:3]]
            texts = [t for _, t in items]
            batches = [texts[i:i + 32] for i in range(0, len(texts), 32)]
            vecs = [v for b in self._pool.map(self.embedder.embed, batches) for v in b]
            with self._lock:
                for t, v in zip(texts, vecs):
                    st["vectors"][t] = v
            scored = [(max(_cos(q, v) for q in qvs), it) for it, v in zip(items, vecs)]
        except Exception:  # lexical fallback
            qw = set().union(*[_words(q) for q in questions])
            scored = [(len(qw & _words(t)) / (1 + math.log(1 + len(t))), it) for it in items]
        return sorted(scored, key=lambda s: -s[0])

    # -- a specific page -------------------------------------------------------------------
    def read(self, run_id: str, url: str, *, question: str = "", budget: int = 8000) -> dict:
        netguard.check_url(url)
        page = self._read(url)
        if not page:
            return {"url": url, "error": "couldn't read that page (it may need a browser, or it's empty)"}
        st = self._state(run_id)
        parts = chunks(page["text"])
        if question and len(page["text"]) > budget:
            ranked = self._rank_passages(st, [question], [(0, c) for c in parts[:80]])
            parts = [t for _, (_, t) in ranked]
        chosen, used = [], 0
        for p in parts:
            if used + len(p) > budget:
                break
            chosen.append(p)
            used += len(p)
        n = self._number(st, page.get("url") or url, page.get("title", ""), page.get("date", ""))
        src = st["sources"][normalize_url(page.get("url") or url)]
        src["passages"].extend(p for p in chosen if p not in src["passages"])
        return {"n": n, "url": page.get("url") or url, "title": page.get("title", ""), "date": page.get("date", ""),
                "text": "\n\n".join(chosen), "truncated": used < len(page["text"]),
                "note": f"Cite this page as [{n}]."}

    # -- after the answer: keep only citations the source supports ---------------------------
    CITE = re.compile(r"(?:\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\](?!\()|【(\d{1,3}(?:\s*[,，]\s*\d{1,3})*)】)")

    @staticmethod
    def normalize_citations(text: str) -> str:
        """【1†L1-L3】, 【1】, 【1, 2】, [1†source] -> [1] / [1, 2]; merges [1][2] -> [1, 2]."""
        def norm(m):
            ns = re.findall(r"(\d{1,3})(?:†[^,，\]】]*)?", m.group(1))
            return "[" + ", ".join(dict.fromkeys(ns)) + "]" if ns else m.group(0)
        text = re.sub(r"【([^】]{1,80})】", norm, text or "")
        text = re.sub(r"\[(\d{1,3}†[^\]]{0,60})\]", norm, text)
        text = re.sub(r"\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\](?:\s*\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\])+",
                      lambda m: "[" + ", ".join(dict.fromkeys(re.findall(r"\d{1,3}", m.group(0)))) + "]", text)
        return text

    def verify_citations(self, run_id: str, text: str) -> tuple[str, dict]:
        text = self.normalize_citations(text)
        st = self._runs.get(run_id)
        stats = {"checked": 0, "removed": 0}
        if not st or not text or not self.CITE.search(text):
            return text, stats
        by_n = {s["n"]: s for s in st["sources"].values()}
        sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|\n", text)
        claims = []
        for s in sentences:
            ns = [int(x) for m in self.CITE.finditer(s) for x in re.split(r"[,，\s]+", m.group(1) or m.group(2)) if x]
            if ns:
                claims.append((s, ns))
        if not claims:
            return text, stats
        plain = [self.CITE.sub("", s).strip() for s, _ in claims]
        try:
            svs = [self.embedder.embed_query(p) for p in plain]
        except Exception:
            svs = [None] * len(plain)
        drop: dict[str, set[int]] = {}
        for (sent, ns), claim, sv in zip(claims, plain, svs):
            for n in ns:
                stats["checked"] += 1
                src = by_n.get(n)
                ok = False
                if src and src["passages"]:
                    passages = " ".join(src["passages"])
                    nums = _numbers(claim) - {str(n)}
                    nums_ok = not nums or bool(nums & _numbers(passages))
                    cw = _words(claim)
                    lexical = len(cw & _words(passages)) / max(1, len(cw))
                    sim = 0.0
                    if sv is not None:
                        vecs = [st["vectors"].get(p) for p in src["passages"]]
                        sim = max((_cos(sv, v) for v in vecs if v), default=0.0)
                    ok = nums_ok and (lexical >= 0.35 or sim >= 0.45)
                if not ok:
                    drop.setdefault(sent, set()).add(n)
        if not drop:
            return text, stats

        def fix(sent: str) -> str:
            bad = drop.get(sent, set())

            def sub(m):
                keep = [x for x in re.split(r"[,，\s]+", m.group(1) or m.group(2)) if x and int(x) not in bad]
                stats["removed"] += len(re.split(r"[,，\s]+", (m.group(1) or m.group(2)).strip())) - len(keep)
                return "[" + ", ".join(keep) + "]" if keep else ""
            return re.sub(r"\s+([.!?,;:])", r"\1", self.CITE.sub(sub, sent)) if bad else sent
        for sent in drop:
            text = text.replace(sent, fix(sent), 1)
        return text, stats
