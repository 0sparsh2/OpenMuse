"""Web tools: search with citations, read a page, weather, raw fetch.

All output is untrusted data (the executor labels it so). Every fetch goes
through search.netguard: public addresses only, redirects re-checked.
"""
from __future__ import annotations

import json
from urllib.parse import quote

from search import netguard
from search.engines import normalize_url
from search.extract import extract
from search.service import WebSearch
from tools.registry import ToolDefinition, ToolRegistry

MAX_CHARS = 12_000
SERVICE: WebSearch | None = None
# user_id -> DuckDuckGo region ("in-en"); set by the API backend from the user's timezone
region_for = None


def service() -> WebSearch:
    global SERVICE
    if SERVICE is None:
        SERVICE = WebSearch()
    return SERVICE


def _site(url: str) -> str:
    from urllib.parse import urlparse
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def register(registry: ToolRegistry) -> None:
    registry.register_namespace("web", "Search the web with citations, read pages, weather.")

    # -- web.search -----------------------------------------------------------------------
    def search(ctx, args):
        region = args.get("region") or ""
        if not region and region_for is not None:
            try:
                region = region_for(getattr(ctx, "user_id", "")) or ""
            except Exception:
                region = ""
        return service().search(ctx.run_id, list(args["queries"]), question=args.get("question", ""),
                                recency_days=args.get("recency_days"), depth=args.get("depth", "normal"),
                                region=region or "wt-wt")

    def search_card(out):
        # compact: a real search has 10+ sources with long URLs; the card must stay under its cap
        return {"type": "web_sources", "queries": out.get("queries", []), "found": out.get("found", 0),
                "read": out.get("read", 0),
                "sources": [{"n": s["n"], "title": (s["title"] or "")[:100], "url": s["url"][:400], "site": s["site"],
                             "date": s.get("date", "")} for s in out.get("sources", [])[:20]]}

    registry.register(ToolDefinition(
        name="web.search", version="1.0.0",
        description=(
            "Search the web and get the most relevant passages from the best pages, as numbered sources [n] "
            "to cite. Write 1-4 short keyword queries covering different angles (add the place, product "
            "name, or year when it matters). Set recency_days for anything that changes (news: 1-7, prices "
            "and releases: 30-90). depth: quick (simple fact), normal (default), research (broad topics). "
            "If the result's note says results are thin, search once more with better queries."),
        input_schema={"type": "object", "properties": {
            "queries": {"type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "string", "maxLength": 200}},
            "question": {"type": "string", "maxLength": 1000,
                         "description": "The user's actual question, in full, so results can be judged against it"},
            "recency_days": {"type": "integer", "minimum": 1, "maximum": 3650},
            "depth": {"type": "string", "enum": ["quick", "normal", "research"]},
            "region": {"type": "string", "maxLength": 8,
                       "description": "DuckDuckGo region like us-en, in-en, uk-en, de-de; defaults to the user's"}},
            "required": ["queries"], "additionalProperties": False},
        output_schema={"type": "object", "required": ["queries", "sources"]},
        capabilities=["network.fetch.public"], side_effect="none", idempotency="pure",
        default_timeout_ms=45_000, data_classes_accepted=["public"], execute=search,
        display=search_card, max_view_chars=17_000, max_display_bytes=12_000,
    ))

    # -- web.read -------------------------------------------------------------------------
    def read(ctx, args):
        return service().read(ctx.run_id, args["url"], question=args.get("question", ""))

    registry.register(ToolDefinition(
        name="web.read", version="1.0.0",
        description=("Read one web page (or PDF) as clean text, e.g. a link the user gave you or a search "
                     "result you need in full. Pass question to get the most relevant parts of long pages. "
                     "The page becomes a numbered source [n] to cite. For pages that need clicking or "
                     "signing in, use the browser instead."),
        input_schema={"type": "object", "properties": {
            "url": {"type": "string", "maxLength": 2000},
            "question": {"type": "string", "maxLength": 1000}},
            "required": ["url"], "additionalProperties": False},
        output_schema={"type": "object"},
        capabilities=["network.fetch.public"], side_effect="none", idempotency="pure",
        default_timeout_ms=30_000, data_classes_accepted=["public"], execute=read,
        display=lambda out: ({"type": "web_sources", "queries": [], "found": 1, "read": 1,
                              "sources": [{"n": out["n"], "title": out.get("title") or _site(out["url"]),
                                           "url": out["url"], "site": _site(out["url"]),
                                           "date": out.get("date", "")}]} if out.get("n") else None),
        max_view_chars=10_000, max_display_bytes=4_000,
    ))

    # -- web.weather (Open-Meteo, keyless) --------------------------------------------------
    def weather(ctx, args):
        loc = args["location"].strip()
        _, st, _, body = netguard.safe_get(
            "https://geocoding-api.open-meteo.com/v1/search?count=1&language=en&format=json&name=" + quote(loc),
            timeout=8)
        places = (json.loads(body or b"{}").get("results") or []) if st == 200 else []
        if not places:
            return {"found": False, "error": f"couldn't find a place called {loc!r}"}
        p = places[0]
        _, st, _, body = netguard.safe_get(
            "https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&timezone=auto&forecast_days=%d"
            "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,precipitation"
            "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
            "&temperature_unit=%s&wind_speed_unit=%s"
            % (p["latitude"], p["longitude"], int(args.get("days", 4)),
               "fahrenheit" if args.get("units") == "imperial" else "celsius",
               "mph" if args.get("units") == "imperial" else "kmh"), timeout=8)
        if st != 200:
            return {"found": False, "error": "weather service unavailable"}
        w = json.loads(body)
        cur, daily = w.get("current", {}), w.get("daily", {})
        unit = "°F" if args.get("units") == "imperial" else "°C"
        days = [{"date": d, "summary": WMO.get(c, "—"), "high": hi, "low": lo, "rain_chance": pr}
                for d, c, hi, lo, pr in zip(daily.get("time", []), daily.get("weather_code", []),
                                            daily.get("temperature_2m_max", []), daily.get("temperature_2m_min", []),
                                            daily.get("precipitation_probability_max", []))]
        place = ", ".join(x for x in (p.get("name"), p.get("admin1"), p.get("country")) if x)
        # the forecast is a citable source like any page, so "[n]" in the answer points somewhere real
        svc = service()
        run_state = svc._state(ctx.run_id)
        url = f"https://open-meteo.com/en/docs#latitude={p['latitude']}&longitude={p['longitude']}"
        n = svc._number(run_state, url, f"Open-Meteo forecast for {place}", (cur.get("time") or "")[:10])
        summary = (f"{place} now: {cur.get('temperature_2m')}{unit}, {WMO.get(cur.get('weather_code'), '—')}. " +
                   " ".join(f"{d['date']}: {d['summary']}, high {d['high']}{unit}, low {d['low']}{unit}, "
                            f"{d['rain_chance']}% chance of rain." for d in days))
        src = run_state["sources"][normalize_url(url)]
        if summary not in src["passages"]:
            src["passages"].append(summary)
        return {"found": True, "n": n, "place": place, "note": f"Cite this forecast as [{n}].",
                "source_url": url, "timezone": w.get("timezone", ""), "unit": unit,
                "now": {"temp": cur.get("temperature_2m"), "feels_like": cur.get("apparent_temperature"),
                        "summary": WMO.get(cur.get("weather_code"), "—"), "humidity": cur.get("relative_humidity_2m"),
                        "wind": cur.get("wind_speed_10m"), "time": cur.get("time")},
                "days": days, "source": "Open-Meteo (open-meteo.com)"}

    registry.register(ToolDefinition(
        name="web.weather", version="1.0.0",
        description="Current weather and a short forecast for a place (city name). Use this instead of web.search for weather.",
        input_schema={"type": "object", "properties": {
            "location": {"type": "string", "maxLength": 120},
            "days": {"type": "integer", "minimum": 1, "maximum": 7},
            "units": {"type": "string", "enum": ["metric", "imperial"]}},
            "required": ["location"], "additionalProperties": False},
        output_schema={"type": "object"},
        capabilities=["network.fetch.public"], side_effect="none", idempotency="pure",
        default_timeout_ms=20_000, data_classes_accepted=["public"], execute=weather,
        display=lambda out: ({"type": "weather", "place": out["place"], "unit": out["unit"], "now": out["now"],
                              "days": out["days"][:5],
                              "source": {"n": out.get("n"), "title": f"Open-Meteo forecast for {out['place']}",
                                         "url": out.get("source_url", "https://open-meteo.com"),
                                         "site": "open-meteo.com", "date": ""}} if out.get("found") else None),
    ))

    # -- web.fetch (raw, kept for compatibility; now network-guarded) -----------------------
    def fetch(ctx, args):
        url = args["url"]
        final, status, ctype, body = netguard.safe_get(url, timeout=15)
        page = extract(body, ctype, final) if body else {"title": "", "text": ""}
        text = page["text"] or body.decode("utf-8", errors="replace")
        return {"url": final, "status": status, "content_type": ctype, "text": text[:MAX_CHARS],
                "truncated": len(text) > MAX_CHARS, "title": page.get("title", "")}

    registry.register(ToolDefinition(
        name="web.fetch", version="1.1.0",
        description="Fetch a public URL and return its readable text. Prefer web.read, which also makes it citable.",
        input_schema={"type": "object", "properties": {"url": {"type": "string", "maxLength": 2000}},
                      "required": ["url"], "additionalProperties": False},
        output_schema={"type": "object",
                       "properties": {"url": {"type": "string"}, "status": {"type": "integer"},
                                      "content_type": {"type": "string"}, "text": {"type": "string"},
                                      "truncated": {"type": "boolean"}},
                       "required": ["url", "status", "content_type", "text", "truncated"]},
        capabilities=["network.fetch.public"], side_effect="none", idempotency="pure",
        default_timeout_ms=20_000, data_classes_accepted=["public"], execute=fetch,
        display=lambda out: {"type": "link", "url": out.get("url", ""), "title": out.get("title") or out.get("url", ""),
                             "status": out.get("status")},
    ))


# WMO weather interpretation codes (Open-Meteo)
WMO = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Freezing fog",
       51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
       61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
       71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Light showers", 81: "Showers",
       82: "Heavy showers", 85: "Snow showers", 86: "Heavy snow showers", 95: "Thunderstorm",
       96: "Thunderstorm with hail", 99: "Thunderstorm with hail"}
