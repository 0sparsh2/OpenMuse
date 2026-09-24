"""
Web search checks — offline (fake engine, fake pages, fake Jev, deterministic
embeddings) plus real Chromium for citations and the Sources sheet.
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.environ.pop("NVIDIA_NIM_API_KEY", None)
os.environ.pop("JEV_API_KEY", None)

from api import ApiBackend                              # noqa: E402
from api.server import serve                            # noqa: E402
from client.serve_ui import serve_ui                    # noqa: E402
from gateway import ModelResponse, ToolCall             # noqa: E402
from memory.embeddings import DeterministicEmbedder    # noqa: E402
from policy import AutonomousDecider                    # noqa: E402
from search import netguard, router                     # noqa: E402
from search.engines import Result, SearchProvider       # noqa: E402
from search.service import WebSearch                    # noqa: E402
from tools.namespaces import web_tools                  # noqa: E402

RESULTS: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


# -- fakes ---------------------------------------------------------------------------------
PAGES = {
    "https://mercury.com/pricing": "<html><title>Mercury Pricing</title><body><article><p>Mercury business checking has "
                                   "no monthly fees and no minimum balance. Mercury Plus costs $35 per month and Mercury Pro "
                                   "costs $350 per month.</p><p>Domestic and international USD wires are free on all plans. "
                                   "Mercury is a fintech company, not a bank; banking services are provided by partner banks.</p>"
                                   "</article></body></html>",
    "https://www.nerdwallet.com/mercury-review?utm_source=x": "<html><title>Mercury Review 2026</title><body><article><p>Mercury "
                                   "charges no account fees on its free plan. Reviewers like the free wires and the clean app, "
                                   "but note there is no cash deposit support.</p><p>Mercury offers up to $5 million in FDIC "
                                   "insurance through sweep networks.</p></article></body></html>",
    "https://science.nasa.gov/mercury": "<html><title>Mercury - NASA</title><body><p>Mercury is the smallest planet in "
                                        "the solar system and the closest to the Sun.</p></body></html>",
    "https://slow.example.com/page": None,   # fetch fails
}


class FakeEngine(SearchProvider):
    name = "fake"

    def __init__(self):
        self.calls = []

    def search(self, query, *, recency_days=None, region="wt-wt", max_results=8):
        self.calls.append((query, recency_days, region))
        if "nothing" in query:
            return []
        if "many" in query:   # a realistic result page: 12 sites, long titles and URLs
            return [Result(f"https://www.news-site-{i}.example.com/sports/cricket/2026/09/16/india-squad-announcement-west-indies-"
                           f"odi-series-rohit-kohli-return-maiden-call-ups-{i:03d}-live-updates-and-analysis",
                           f"India squad for West Indies ODIs: Rohit, Kohli return; maiden call-ups for Nabi and Dhir ({i})",
                           "India have named their ODI squad for the West Indies series starting 27 September.")
                    for i in range(12)]
        rs = [Result("https://mercury.com/pricing", "Mercury Pricing | Plans", "Mercury pricing: free, Plus $35/mo, Pro $350/mo."),
              Result("https://www.nerdwallet.com/mercury-review?utm_source=x", "Mercury Review 2026 - NerdWallet",
                     "An honest review of Mercury business banking fees."),
              Result("https://science.nasa.gov/mercury", "Mercury - NASA Science", "The smallest planet in our solar system."),
              Result("https://slow.example.com/page", "Mercury bank fees explained", "Fees and limits for Mercury accounts.")]
        if "pricing" in query:   # a second query returns the same review with a different tracking param
            rs.insert(0, Result("https://nerdwallet.com/mercury-review?utm_source=y", "Mercury Review 2026", "fees"))
        return rs


class FakeJev:
    available = True

    def __init__(self):
        self.calls = []

    def scores(self, state, questions, levels, **kw):
        self.calls.append(("scores", state))
        out = {}
        for k in questions:
            txt = state[k].lower()
            out[k] = 0.2 if ("planet" in txt or "nasa" in txt) else 2.8
        return out

    def noul(self, state, question, **kw):
        self.calls.append(("noul", state))
        if "user_message" in state:
            return 0.93 if "pixel" in state["user_message"].lower() else 0.04
        return 0.2 if "thin" in state.get("question", "") else 0.9


def web_tools_registry():
    from tools.registry import ToolRegistry
    reg = ToolRegistry()
    web_tools.register(reg)
    return reg


def fake_fetch(url, timeout=5.0, **kw):
    html = PAGES.get(url)
    if html is None:
        raise TimeoutError("slow")
    return url, 200, "text/html; charset=utf-8", html.encode()


def main() -> int:
    # -- 1. network guard ------------------------------------------------------------------
    for url, why in [("http://127.0.0.1:8765/v1/sessions", "loopback"), ("http://localhost/", "localhost"),
                     ("http://10.0.0.5/admin", "private range"), ("http://169.254.169.254/latest/meta-data", "cloud metadata"),
                     ("http://[::1]:8080/", "IPv6 loopback"), ("http://printer.local/", ".local name"),
                     ("http://intranet/", "bare hostname"), ("ftp://example.com/x", "not http"),
                     ("https://user:pw@example.com/", "embedded credentials"), ("http://0.0.0.0:8765/", "unspecified")]:
        try:
            netguard.check_url(url); ok = False
        except netguard.BlockedURL:
            ok = True
        check(f"web tools refuse {why}", ok)

    # a redirect hop to a private address is refused too
    class Redir(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302); self.send_header("Location", "http://127.0.0.1:8765/v1/sessions"); self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redir)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    real_check = netguard.check_url
    netguard.check_url = lambda u: "ok" if f":{port}/" in u else real_check(u)   # pretend only the first hop is public
    try:
        netguard.safe_get(f"http://127.0.0.1:{port}/go"); ok = False
    except netguard.BlockedURL:
        ok = True
    netguard.check_url = real_check
    srv.shutdown()
    check("a redirect into the private network is refused", ok)

    # -- 2. the search pipeline --------------------------------------------------------------
    eng, jev = FakeEngine(), FakeJev()
    ws = WebSearch(engines=[eng], jev=jev, embedder=DeterministicEmbedder(), fetch=fake_fetch)
    r = ws.search("run_a", ["mercury bank fees", "mercury pricing plans", "mercury bank fees"],
                  question="What does Mercury business banking cost?", recency_days=30, region="us-en")
    check("queries fan out in parallel, de-duplicated", sorted(q for q, _, _ in eng.calls) ==
          ["mercury bank fees", "mercury pricing plans"] and all(c[1] == 30 and c[2] == "us-en" for c in eng.calls))
    urls = [s["url"] for s in r["sources"]]
    check("the same page from two queries (different tracking tags) appears once",
          sum("nerdwallet" in u for u in urls) == 1, str(urls))
    check("Jev decides which results matter: the NASA page is dropped", r["ranked_by"] == "jev"
          and not any("nasa" in u for u in urls), str(urls))
    opened = [s["site"] for s in r["sources"] if s["opened"]]
    check("relevant pages are opened and read; a failing page is skipped", "mercury.com" in opened
          and "nerdwallet.com" in opened and "slow.example.com" not in opened, str(opened))
    passages = " ".join(p for s in r["sources"] for p in s["passages"])
    check("answers come from the page text, not just snippets", "$350 per month" in passages and "partner banks" in passages)
    check("sources are numbered for citing", [s["n"] for s in r["sources"]] == list(range(1, len(r["sources"]) + 1)))
    check("passages fit the context budget", len(passages) <= 8500)
    check("results judged good enough → cite as usual", r["enough"] == 0.9 and "Cite sources" in r["note"])
    r2 = ws.search("run_a", ["mercury fdic insurance"], question="thin question")
    same = {s["url"]: s["n"] for s in r["sources"]}
    check("a second search in the same answer keeps the same numbers",
          all(same.get(s["url"], s["n"]) == s["n"] for s in r2["sources"]))
    check("thin results → told to search once more", "search again once" in r2["note"])
    r3 = ws.search("run_b", ["mercury bank fees"])
    check("another answer starts numbering at [1]", r3["sources"][0]["n"] == 1)
    check("no results → advice to rephrase", "No results" in ws.search("run_c", ["nothing here"])["note"])
    ws_nojev = WebSearch(engines=[eng], jev=type("J", (), {"available": False})(), embedder=DeterministicEmbedder(),
                         fetch=fake_fetch)
    r4 = ws_nojev.search("run_d", ["mercury bank fees"], question="What does Mercury cost?")
    check("without Jev, embeddings rank the results instead", r4["ranked_by"] == "embeddings" and r4["sources"])
    rd = ws.read("run_a", "https://mercury.com/pricing", question="plus plan price")
    check("reading a specific page makes it a citable source", rd["n"] == same["https://mercury.com/pricing"]
          and "Mercury Plus costs $35" in rd["text"])
    try:
        ws.read("run_a", "http://127.0.0.1:8765/v1/sessions"); ok = False
    except netguard.BlockedURL:
        ok = True
    check("web.read refuses private addresses", ok)

    # -- 3. citation check ---------------------------------------------------------------------
    n_mer, n_nw = same["https://mercury.com/pricing"], next(v for k, v in same.items() if "nerdwallet" in k)
    answer = (f"Mercury's basic business checking has no monthly fees [{n_mer}]. Mercury Plus costs $35 per month "
              f"and Pro is $350 [{n_mer}]. Mercury Plus costs $49 per month [{n_nw}]. It was founded on the Moon [{n_nw}]. "
              f"Wires are free on every plan [{n_mer}, 9].")
    fixed, stats = ws.verify_citations("run_a", answer)
    check("supported citations stay", f"no monthly fees [{n_mer}]" in fixed and f"$350 [{n_mer}]" in fixed, fixed)
    check("a citation whose numbers don't match the source is removed", f"$49 per month [{n_nw}]" not in fixed, fixed)
    check("a citation the source doesn't support is removed", "on the Moon." in fixed and f"Moon [{n_nw}]" not in fixed, fixed)
    check("a made-up source number is removed", "[9]" not in fixed and ", 9" not in fixed and f"plan [{n_mer}]" in fixed, fixed)
    check("citation stats are recorded", stats["checked"] == 6 and stats["removed"] == 3, str(stats))
    norm = WebSearch.normalize_citations("No monthly fee【1†L1-L3】【2†L1-L4】. Pro is $299【1】. See [docs](https://x.io) [2][3].")
    bogus = WebSearch.normalize_citations('High 29.4°C【{"id": "bbdba5b1-67e4"}】. Low 22°C【3†L2】.')
    check("markers that aren't source numbers are removed (not misread as [5, 1])",
          bogus == "High 29.4°C. Low 22°C[3].", bogus)
    check("ChatGPT-style markers (【1†L1-L3】) become [1]", norm ==
          "No monthly fee[1, 2]. Pro is $299[1]. See [docs](https://x.io) [2, 3].", norm)
    from api.voice import speakable
    body, listed = WebSearch.split_sources_line("India's squad:\n\n- Shubman Gill (Captain)\n- Rohit Sharma\n\nSources: [1], [2], [3], [4].")
    check("a model-written 'Sources: [1], [2]…' line is taken out (the app lists sources)",
          body == "India's squad:\n\n- Shubman Gill (Captain)\n- Rohit Sharma" and listed == {1, 2, 3, 4}, repr(body))
    check("…including bold variants, but not sentences that mention sources",
          WebSearch.split_sources_line("**Sources:** [1, 2] and [5]") == ("", {1, 2, 5})
          and WebSearch.split_sources_line("The sources say it's sunny [1].")[1] == set())
    check("a sources line with a note keeps the note as a sentence",
          WebSearch.split_sources_line(WebSearch.normalize_citations(
              "Sources: [1][2][3] (all dated 16 Sept 2026, reporting the BCCI announcement).")) ==
          ("All dated 16 Sept 2026, reporting the BCCI announcement.", {1, 2, 3}))
    check("…while prose that starts with 'Sources' is left alone",
          WebSearch.split_sources_line("Sources say it's sunny [1].")[1] == set()
          and WebSearch.split_sources_line("Sources: the council's 2025 report.")[1] == set())
    check("removed markers never leave ',,,' or an empty 'Sources:' line",
          WebSearch.tidy("Kabaddi 49-24 , , .\n\nSources:,,,.") == "Kabaddi 49-24.")
    stale = ws.search("run_y", ["india squad west indies odi 2024"], question="upcoming India squad for the West Indies ODIs")
    check("a past year in a query about something upcoming gets a 'today is …' warning", "today is" in stale["note"]
          and "2024" in stale["note"], stale["note"])
    check("markers are never read aloud", speakable("No fee【1†L1-L3】 and wires are free [2, 3].") ==
          "No fee and wires are free.")
    from gateway.providers.openai_compat import _coerce_json_strings
    schema = next(t for t in web_tools_registry().load_namespace("web") if t["name"] == "web.search")["input_schema"]
    fixed_args = _coerce_json_strings({"queries": "[latest F1 race winner]", "depth": "", "recency_days": None}, schema)
    check("sloppy tool arguments are repaired instead of failing", fixed_args == {"queries": ["latest F1 race winner"]},
          str(fixed_args))

    # -- 4. router --------------------------------------------------------------------------------
    rj = FakeJev()
    cases = {"write a haiku about autumn": False, "what's 15% of 240": False,
             "is it safe to take ibuprofen with lisinopril?": True, "who won the F1 race last weekend?": True,
             "will it rain in pune tomorrow": True, "any good ramen near shibuya open late?": True,
             "summarize https://example.com/post": True}
    got = {t: router.decide(t, jev=rj).search for t in cases}
    check("rules settle the clear cases without calling Jev", got == cases and rj.calls == [], str(got))
    h = router.decide("what's the price of the pixel 11 in india", jev=rj)
    check("unclear cases go to Jev (calibrated yes/no)", h.by == "jev" and h.search and h.p == 0.93 and len(rj.calls) == 1)
    h2 = router.decide("explain how vaccines train the immune system", jev=rj)
    check("…including the high-stakes ones rules still catch", h2.search and h2.high_stakes and "professional" in router.note_for(h2))
    check("weather goes to the weather tool", "web.weather" in router.note_for(router.decide("weather in Pune?")))
    check("a shared link is read", "web.read" in router.note_for(router.decide("what does https://ex.com/a say?")))
    hr = router.decide("research the best note-taking apps for students", jev=rj)
    check("research requests get a deep, multi-angle search plan", hr.research and 'depth="research"' in router.note_for(hr))
    check("…but editing text that mentions a report doesn't", not router.decide("rewrite this report on sales").search)

    # a saved report keeps its citations meaningful
    from api.library import _with_web_sources
    web_tools.SERVICE = ws
    rep = _with_web_sources("run_a", f"# Mercury costs\n\nThe base plan is free【{same['https://mercury.com/pricing']}†L1】.")
    check("saving a report appends the cited sources with links",
          "## Sources" in rep and "https://mercury.com/pricing" in rep and "†" not in rep and "nerdwallet" not in rep, rep)

    # -- 5. in the agent loop -----------------------------------------------------------------------
    tmp = tempfile.mkdtemp(prefix="openmuse-search-")
    web_tools.SERVICE = ws
    import search.jev as J
    J._DEFAULT = rj
    SEEN = []
    PLAN = {"calls": [], "answer": "Done."}

    def respond(request, history):
        SEEN.append(request)
        tools = [m for m in request.messages if m.role == "tool"]
        if len(tools) < len(PLAN["calls"]):
            name, args = PLAN["calls"][len(tools)]
            return ModelResponse(text="", stop_reason="tool_calls", tool_calls=[ToolCall(id=f"w{len(tools)}", name=name, arguments=args)])
        nums = {}
        if tools:  # cite by the numbers the search actually returned, like a real model would
            body = tools[-1].blocks[0].text
            for site in ("mercury.com", "nerdwallet.com"):
                import re as _re
                m = _re.search(r'"n":\s*(\d+)[^{}]*?"site":\s*"' + _re.escape(site), body)
                nums[site.split(".")[0]] = m.group(1) if m else "0"
            m = _re.search(r'"n":\s*(\d+)', body)
            nums["wx"] = m.group(1) if m else "0"
        return ModelResponse(text=PLAN["answer"].format(**nums) if nums else PLAN["answer"], stop_reason="stop")

    backend = ApiBackend(workspace_root=os.path.join(tmp, "ws"), respond=respond, db_path=os.path.join(tmp, "db.sqlite"),
                         accounts_root=os.path.join(tmp, "acct"))
    backend.decider = AutonomousDecider()

    def ask(uid, text):
        SEEN.clear()
        chat = backend.create_session(user_id=uid).chat_id
        run, _, _ = backend.submit_message(chat_id=chat, user_id=uid, content=[{"type": "text", "text": text}])
        t0 = time.time()
        while run.state not in ("COMPLETED", "FAILED") and time.time() - t0 < 20:
            time.sleep(0.05)
        return run

    def first_prompt():
        return " ".join(b.text for m in SEEN[0].messages for b in m.blocks) if SEEN else ""
    ask("usr_a", "write a haiku about autumn")
    check("web search is available on every turn", any(t.name == "web.search" for t in SEEN[0].tools))
    check("no search nudge for writing tasks", "Runtime note — trusted] This likely" not in first_prompt())
    ask("usr_a", "what's the price of the pixel 11 in india")
    check("the router nudges the first step to search when needed", "Call web.search" in first_prompt())
    backend.set_search_prefs("usr_b", auto=False)
    ask("usr_b", "what's the price of the pixel 11 in india")
    check("with automatic search off, no nudge (the model can still search when asked)",
          "Call web.search" not in first_prompt() and any(t.name == "web.search" for t in SEEN[0].tools))

    PLAN["calls"] = [("web.search", {"queries": ["mercury bank fees", "mercury pricing plans"],
                                     "question": "What does Mercury cost?", "recency_days": 90})]
    PLAN["answer"] = ("Mercury's basic business checking has no monthly fees [{mercury}]. Mercury Plus costs $35 per month "
                      "and Pro is $350 [{mercury}]. It was founded on the Moon [{nerdwallet}].")
    run = ask("usr_a", "how much does mercury business banking cost?")
    evts = backend.eventbus.read_since(run.run_id, -1)
    disp = next((e.data["display"] for e in evts if e.type == "tool.result" and e.data.get("display")), {})
    check("the search result reaches the UI as a sources card", disp.get("type") == "web_sources"
          and disp["queries"] == ["mercury bank fees", "mercury pricing plans"] and disp["sources"])
    final = "".join(e.data.get("text", "") for e in evts if e.type == "assistant.delta")
    # a real-sized result: the sources card must still reach the app (it used to be dropped over 2 KB)
    PLAN["calls"] = [("web.search", {"queries": ["many results squad"], "question": "upcoming India ODI squad"})]
    saved_answer = PLAN["answer"]
    PLAN["answer"] = "India named a 15-player squad."
    run_many = ask("usr_a", "india odi squad for west indies")
    card = next((e.data.get("display") for e in backend.eventbus.read_since(run_many.run_id, -1)
                 if e.type == "tool.result"), None) or {}
    check("a real-sized search (12 sites, long URLs) still sends its sources card to the app",
          len(card.get("sources", [])) >= 10 and len(json.dumps(card)) > 2048, str(len(card.get("sources", []))))
    PLAN["calls"] = [("web.search", {"queries": ["mercury bank fees", "mercury pricing plans"],
                                     "question": "What does Mercury cost?", "recency_days": 90})]
    PLAN["answer"] = saved_answer
    inline_answer = PLAN["answer"]
    PLAN["answer"] = "Mercury's plans:\n\n- Basic checking with no monthly fees\n- Plus at $35 per month\n\nSources: [{mercury}], [{nerdwallet}]."
    run_list = ask("usr_a", "list mercury's plans")
    evts2 = backend.eventbus.read_since(run_list.run_id, -1)
    final2 = "".join(e.data.get("text", "") for e in evts2 if e.type == "assistant.delta")
    flags = {x["site"]: x["cited"] for x in backend.run_sources(run_list.run_id)}
    check("an answer that lists its sources at the end: the line goes, the sources count as cited",
          "Sources" not in final2 and ",," not in final2 and flags.get("mercury.com") and flags.get("nerdwallet.com"),
          f"{final2!r} {flags}")
    PLAN["answer"] = inline_answer
    check("unsupported citations are removed before you see the answer",
          final.count("[") == 2 and "Moon." in final and backend.citation_stats[run.run_id]["removed"] == 1, final)

    # -- 6. real Chromium: step, chips, Sources sheet, weather card -----------------------------------
    api_srv = serve(backend)
    api = f"http://127.0.0.1:{api_srv.server_address[1]}"
    import urllib.request
    req = urllib.request.Request(api + "/v1/auth/signup", method="POST", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"email": "s@example.com", "password": "correct horse 42", "name": "Sam"}).encode())
    res = json.loads(urllib.request.urlopen(req).read())
    token = res["token"]
    ui = serve_ui(api, domains_root=os.path.join(tmp, "ui"), require_auth=True)
    base = f"http://localhost:{ui.server_address[1]}"
    real_safe_get = web_tools.netguard.safe_get

    def fake_weather(url, timeout=8, **kw):
        if "geocoding" in url:
            return url, 200, "application/json", json.dumps({"results": [{"name": "Pune", "admin1": "Maharashtra",
                "country": "India", "latitude": 18.5, "longitude": 73.8}]}).encode()
        return url, 200, "application/json", json.dumps({"timezone": "Asia/Kolkata", "current": {
            "temperature_2m": 27.4, "apparent_temperature": 30.1, "relative_humidity_2m": 78, "weather_code": 61,
            "wind_speed_10m": 11, "time": "2026-09-24T10:00"}, "daily": {"time": ["2026-09-24", "2026-09-25"],
            "weather_code": [61, 3], "temperature_2m_max": [29, 30], "temperature_2m_min": [22, 22],
            "precipitation_probability_max": [80, 30]}}).encode()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context()
        ctx.add_init_script(f"try {{ localStorage.setItem('om.token', {json.dumps(token)}); }} catch (e) {{}}")
        page = ctx.new_page()
        page.goto(base + "/")
        page.fill("textarea[aria-label='Message']", "how much does mercury business banking cost?")
        page.keyboard.press("Enter")
        page.wait_for_selector("a.cite", timeout=20000)
        step = page.inner_text(".mc-search > summary")
        check("the search shows as one step with its searches and sites read", "Searched the web · 2 searches · read" in step, step)
        chips = page.eval_on_selector_all("a.cite", "els => els.map((e) => [e.textContent, e.href])")
        check("citations render as site chips linking to the source", chips and chips[0][0] == "mercury.com"
              and chips[0][1].startswith("https://mercury.com/pricing"), str(chips))
        check("removed citations don't show", len(chips) == 2 and "[2]" not in page.inner_text(".mc-bubble.bot"))
        page.click(".mc-search > summary")
        qs = page.eval_on_selector_all(".mc-query", "els => els.map((e) => e.textContent)")
        check("the actual queries are visible", qs == ["mercury bank fees", "mercury pricing plans"], str(qs))
        row = page.eval_on_selector_all(".mc-srcrow .mc-srcchip:not(.mc-srcchip-all)", "els => els.map((e) => [e.textContent, e.href])")
        check("the websites an answer used are listed under it", row and row[0][0].endswith("mercury.com")
              and row[0][1].startswith("https://mercury.com/pricing"), str(row))
        chats = json.loads(urllib.request.urlopen(urllib.request.Request(api + "/v1/chats", headers={"Authorization": "Bearer " + token})).read())["chats"]
        turns = json.loads(urllib.request.urlopen(urllib.request.Request(api + f"/v1/chats/{chats[0]['chat_id']}/messages",
                                                                    headers={"Authorization": "Bearer " + token})).read())["turns"]
        srcs = turns[-1].get("sources") or []
        check("sources are saved with the answer (chat history API)", srcs and any(x["cited"] and x["site"] == "mercury.com" for x in srcs)
              and any(not x["cited"] for x in srcs), str(srcs)[:300])
        page.evaluate("() => { for (const k of Object.keys(localStorage)) if (k !== 'om.token') localStorage.removeItem(k); }")
        page.reload()
        page.wait_for_selector(".mc-srcrow", timeout=15000)
        bubble = page.inner_text(".mc-bubble.bot")
        check("reopening the chat later still shows the websites (and no bare [n])",
              page.eval_on_selector_all("a.cite", "els => els.length") == 2 and "[1" not in bubble
              and "mercury.com" in page.inner_text(".mc-srcrow"), bubble[:200])
        page.click(".mc-srcchip-all")
        page.wait_for_selector(".src-sheet")
        groups = page.eval_on_selector_all(".src-sheet-g", "els => els.map((e) => e.textContent)")
        check("the Sources sheet separates cited from also-read", groups == ["Cited", "Also read"], str(groups))
        page.click(".src-sheet .bv-x")

        web_tools.netguard.safe_get = fake_weather
        PLAN["calls"] = [("web.weather", {"location": "Pune"})]
        PLAN["answer"] = "Light rain in Pune right now, about 27.4°C [{wx}]."
        page.fill("textarea[aria-label='Message']", "will it rain in pune today?")
        page.keyboard.press("Enter")
        page.wait_for_selector(".mc-wx-now", timeout=20000)
        wx = page.inner_text(".mc-card")
        check("weather shows as a card with a forecast", "Pune, Maharashtra, India" in wx and "27°C" in wx and "80% rain" in wx, wx)
        page.wait_for_function("document.querySelectorAll('a.cite').length > 2", timeout=10000)
        wx_chip = page.eval_on_selector_all("a.cite", "els => els.map((e) => e.textContent)")[-1]
        check("the forecast is cited like any other source", wx_chip == "open-meteo.com", wx_chip)
        web_tools.netguard.safe_get = real_safe_get

        page.click("#tabbar button[data-tab='connectors']")
        page.wait_for_selector("#searchAuto")
        check("Apps shows the automatic-search setting", page.is_checked("#searchAuto"))
        browser.close()

    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
