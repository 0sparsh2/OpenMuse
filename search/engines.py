"""
Search engines behind one interface. DuckDuckGo (keyless) today; Tavily,
Brave, Exa or SearXNG slot in as more SearchProvider classes.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode


@dataclass
class Result:
    url: str
    title: str
    snippet: str
    date: str = ""
    source: str = ""          # engine name
    kind: str = "web"         # web | news
    ranks: dict = field(default_factory=dict)   # query -> rank (for fusion)

    @property
    def site(self) -> str:
        host = (urlparse(self.url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host


class SearchProvider:
    name = "base"

    def search(self, query: str, *, recency_days: int | None = None, region: str = "wt-wt",
               max_results: int = 8) -> list[Result]:
        raise NotImplementedError


def _timelimit(days: int | None) -> str | None:
    if not days:
        return None
    if days <= 1:
        return "d"
    if days <= 7:
        return "w"
    if days <= 31:
        return "m"
    if days <= 366:
        return "y"
    return None


class DuckDuckGo(SearchProvider):
    """Keyless. Unofficial endpoints: rate limits and occasional breakage are
    expected, so callers get [] rather than an exception, and a short breaker
    stops hammering it after failures."""
    name = "duckduckgo"

    def __init__(self):
        self._fail_until = 0.0
        self._lock = threading.Lock()

    def search(self, query, *, recency_days=None, region="wt-wt", max_results=8):
        if time.time() < self._fail_until:
            return []
        from ddgs import DDGS
        tl = _timelimit(recency_days)
        out: list[Result] = []
        try:
            with DDGS(timeout=8) as d:
                for r in d.text(query, region=region, safesearch="moderate", timelimit=tl,
                                max_results=max_results) or []:
                    if r.get("href"):
                        out.append(Result(url=r["href"], title=r.get("title", ""), snippet=r.get("body", ""),
                                          source=self.name))
                if recency_days and recency_days <= 7:       # breaking topics: add news results with dates
                    for r in d.news(query, region=region, safesearch="moderate", timelimit=tl,
                                    max_results=5) or []:
                        if r.get("url"):
                            out.append(Result(url=r["url"], title=r.get("title", ""), snippet=r.get("body", ""),
                                              date=str(r.get("date", ""))[:10], source=self.name, kind="news"))
        except Exception:
            with self._lock:
                self._fail_until = time.time() + 20
            return out
        return out


_TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_src|igshid|si$)")


def normalize_url(url: str) -> str:
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)]
    host = (p.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return urlunparse((p.scheme.lower() or "https", host + (f":{p.port}" if p.port else ""),
                       p.path.rstrip("/") or "/", "", urlencode(q), ""))


def fuse(result_lists: dict[str, list[Result]], k: int = 60) -> list[Result]:
    """Reciprocal-rank fusion across queries (and engines), de-duplicated by URL."""
    merged: dict[str, Result] = {}
    score: dict[str, float] = {}
    for q, results in result_lists.items():
        for rank, r in enumerate(results):
            key = normalize_url(r.url)
            if key not in merged:
                merged[key] = r
            else:  # keep the richer snippet / a date if one list had it
                m = merged[key]
                if len(r.snippet) > len(m.snippet):
                    m.snippet = r.snippet
                m.date = m.date or r.date
            merged[key].ranks[q] = rank
            score[key] = score.get(key, 0.0) + 1.0 / (k + rank + 1)
    return [merged[u] for u in sorted(merged, key=lambda u: -score[u])]
