# Web search

OpenMuse searches the web the way ChatGPT does. It decides when a question needs
the web, writes several focused queries, reads the best pages, and answers with
citations you can click. It uses DuckDuckGo, so no search API key is needed.
Jev (TypeSafe AI), when configured, makes the fast in-between decisions.

## What you see

- A step, **"Searched the web · 3 searches · read 4 sites"**, with site icons.
  Expanding it shows the exact queries and the numbered sources.
- **Citation chips** in the answer (e.g. `mercury.com +1`). Each links to the
  page it cites.
- A **Sources** row under every searched answer lists the websites it cites,
  numbered to match the chips. "All N sources" opens a panel split into
  **Cited** and **Also read**. Sources are saved with the answer (SQLite), so a
  chat reopened later still shows them; the chat history API returns them as
  `turns[].sources`.
- **Weather** answers show a forecast card. The forecast (Open-Meteo) is cited like
  any other source.
- **Research** requests ("research…", "compare the best…", "write a report") run a
  deeper, multi-angle search. Reports saved with `docs.create` get a Sources list
  added automatically.
- In **voice mode** OpenMuse says "Let me look that up" and doesn't read the
  citation markers aloud.

**Apps → Web search → Search automatically** turns automatic search on or off.
When it's off, OpenMuse searches only when you ask.

## Tools the model has

| Tool | Risk | What it does |
|---|---|---|
| `web.search` | R0 | 1–4 queries, optional `recency_days`, `depth` (quick, normal, research), `region`. Returns numbered sources with the most relevant passages. |
| `web.read` | R0 | Read one page or PDF as clean text; it becomes a numbered source. For links the user shares. |
| `web.weather` | R0 | Current weather and a forecast from Open-Meteo (keyless), as a citable source. |
| `web.fetch` | R0 | Raw fetch returning readable text (kept for compatibility; prefer `web.read`). |

The `web` namespace is loaded on every turn. Guidance for the model is in
`prompts/tool-choice-addendum.md`: when to search, how to write queries, and how
to cite.

## How a search works (`search/service.py`)

1. **Fan out.** The model's queries (duplicates removed) run in parallel on every
   engine. `recency_days` maps to the engine's time filter; for 7 days or fewer,
   dated news results are added. The region comes from the user's timezone
   (e.g. `Asia/Kolkata` → `in-en`) unless the model sets one.
2. **Merge.** Reciprocal-rank fusion across queries. URLs are normalised (tracking
   parameters and `www.` stripped), so one page never appears twice.
3. **Choose what matters.** Jev scores every result's usefulness for the user's
   question in a single call (≈0.4s). Off-topic results are dropped. Without Jev,
   NIM embeddings rank them; failing that, the engine's order is used.
4. **Read.** The top pages are fetched in parallel through the network guard:
   2 for quick, 4 for normal, 7 for research, at most 2 per site. Readable text
   and the publish date are extracted (trafilatura; pypdf for PDFs). Slow or
   failing pages are skipped rather than waited on.
5. **Pick passages.** Pages are split into ~800-character passages and ranked with
   NIM embeddings against the question and queries. Snippets from unopened results
   are included too. Passages are chosen best-per-source first, then by rank,
   within a character budget: 4.5k, 8.5k or 15k.
6. **Number the sources.** Sources get `[n]` numbers that stay the same across
   every search in one answer.
7. **Enough?** Jev judges whether the passages answer the question well. If they
   don't (a score below 0.35), the result tells the model to search once more with
   better queries.

## When it searches: the router (`search/router.py`)

The router runs in the background while the context is built, so it adds no
waiting time. It never blocks a turn for more than about 1.2 seconds.

1. **Rules** (instant) settle the clear cases.
   - **Search:** explicit asks ("look up", "latest", "sources?"), time words plus
     volatile topics ("price … today", "who won …"), local questions ("near me",
     "open late"), pasted links, research requests, and health, legal or money
     questions.
   - **Don't search:** writing, translating, maths, small talk, and summarising text
     the user pasted.
2. **Jev** decides the unclear cases with a calibrated yes/no (search at ≥ 0.6).
3. When search is clearly needed, a trusted note on the first step tells the model
   to search first. It includes a suggested recency, "use web.weather" for weather,
   "use web.read" for a pasted link, or a research plan. Health, legal and money
   questions always get "check authoritative sources and cite them". Those were
   the case Jev alone missed in testing.

The model still makes the final call: `web.search` is always available.

## Citations (`WebSearch.verify_citations`)

Before the answer is shown:
- Markers are normalised. ChatGPT-style `【1†L1-L3】` becomes `[1]`, and `[1][2]`
  becomes `[1, 2]`. Anything inside `【】` that isn't a source number, such as a
  tool id, is removed.
- Each cited sentence is compared with its source's passages. A citation stays only
  if the wording overlaps (≥ 35%) or the sentence is semantically similar (cosine ≥
  0.45), **and** any numbers in the sentence appear in the source.
- Unsupported or made-up citation numbers are removed. The sentence itself stays;
  only the chip goes. The counts are recorded per run (`citation_stats`).

## Jev (TypeSafe AI)

Jev is a "decision model": it takes text and returns calibrated numbers, never
generated text. OpenMuse calls it through `search/jev.py`.

- **Timeouts.** Each call has a 2–2.5s timeout.
- **Caching.** Identical questions are cached for 10 minutes.
- **Circuit breaker.** After an error it backs off for 30 seconds, or an hour on
  a 401.
- **Fallbacks.** Every caller has one, so Jev being down makes things slower, never
  broken. It only runs when `JEV_API_KEY` is set.

Where it's used:

| Decision | Question type | Fallback |
|---|---|---|
| Which search results matter | one score per result, all in one call | embeddings → engine order |
| Are the results enough? | yes/no | no check |
| Does this turn need a search? | yes/no | rules only |
| Browser: what kind of page is this, and is the task done? | choice + yes/no, once per page | keyword checks only |
| Browser: does this button commit to something? | yes/no, only for buttons or on form/checkout pages | keyword check only |

Not used for: freshness or date decisions, which are weak spots for Jev in both
TypeSafe's documentation and Parallel's tests.

### In the live browser

When Jev is configured, every new page gets `observation.page`
(`{"kind", "confidence", "task_done"}`) and `observation.hints`:

| Page kind | Hint to the model |
|---|---|
| Cookie banner | Dismiss it first, preferring "Reject all" or "Necessary only" |
| Sign-in wall | Use a saved login (`browser.logins`) or ask the user to sign in in the live view |
| Error page | Go back or try a different result |
| Checkout | Anything that pays or books needs the user's approval |
| Bot check (confidence ≥ 0.85) | The session pauses for the user, like a detected CAPTCHA |
| `task_done` ≥ 0.8 | Stop clicking and report what's on the page |

Loop detection is a rule and doesn't need Jev: the same action on the same page 3
times adds a "try something else" hint.

## Security

- **Network guard** (`search/netguard.py`), for every web tool and every redirect:
  - Only http(s) on public addresses. Loopback, private ranges, link-local (including
    cloud metadata), multicast, reserved, bare hostnames, and `.local`/`.internal`
    names are refused.
  - URLs with embedded credentials are refused.
  - No proxies from the environment and no cookies.
  - 3 MB and time caps.
- **Untrusted data.** Page text reaches the model as untrusted data. The prompt
  says never to follow instructions found in pages; the injection and taint checks
  still apply.
- **Private queries.** Queries leave the machine, so the guidance tells the model
  not to put private details in queries unless the request needs them. The queries
  are shown to the user in the step.

## Engines

The interface is `search/engines.py::SearchProvider.search(query, recency_days, region, max_results)`.

| Engine | Status |
|---|---|
| DuckDuckGo (`ddgs`) | In use. Keyless, with web and news results. Unofficial endpoints, so a short circuit breaker handles rate limits. |
| Tavily, Brave, Exa, SearXNG | Drop-in candidates: add a `SearchProvider` and pass `WebSearch(engines=[...])`. Results from several engines are merged by the same rank fusion. |

## Measured behaviour

| What | Result |
|---|---|
| Search service alone (live DuckDuckGo, Jev, NIM embeddings) | 3–6s per search |
| Whole answers with the live model | Pricing question ~20s (3 queries); latest F1 result ~28s (3 searches after Jev flagged thin results); research comparison ~106s with 8 verified citations |
| Jev on the local test pages (`demo_cu_jev.py`) | Correct on cookie banner, sign-in wall, results page, "task done", a payment "Continue" the keyword check missed, and a disguised human check |
| Tests | `demo_search.py` 60/60, `demo_cu_jev.py` 16/16 |

Most of the time in a searched answer goes to the main model's reasoning between
steps, not to the search itself.

**Next improvements:**
- A labelled set of ~150 questions to measure search-or-not accuracy, citation
  accuracy and freshness.
- A second engine, for when DuckDuckGo rate-limits.
