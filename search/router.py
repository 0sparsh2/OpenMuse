"""
Search router: does this turn need the web, and how fresh?

Layer 0 — rules (<1 ms): explicit asks, time words, volatile topics, local
questions, pasted links, high-stakes topics; and obvious "no" cases
(writing, maths, translation, text the user pasted).
Layer 1 — Jev (≈0.4 s, run in parallel with context building): a
calibrated yes/no when the rules aren't conclusive.
The main model still decides in the end (web.search is always available);
the router only adds a trusted nudge on the first step when search is
clearly needed. High-stakes topics (health, law, money) always get one,
because that's where Jev alone was weakest in our tests.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

EXPLICIT = re.compile(r"\b(search|google|look (it |that |this )?up|browse|find (me )?(online|on the web)|"
                      r"what('s| is| are) the latest|latest|sources?\??$|cite|citations?|verify|fact[- ]check|"
                      r"are you sure)\b", re.I)
TIME = re.compile(r"\b(today|tonight|tomorrow|yesterday|this (week|weekend|month|year)|last (night|week|weekend|month)|"
                  r"right now|currently|current|recent(ly)?|upcoming|next (week|game|match|release)|"
                  r"20(2[5-9]|3\d)|breaking|news)\b", re.I)
VOLATILE = re.compile(r"\b(price|prices|cost|costs|how much|deal|discount|stock|share price|crypto|bitcoin|"
                      r"exchange rate|interest rate|score|scores|standings|results?|who won|winner|schedule|"
                      r"release date|launch(ed)?|version|update|election|polls?|ceo of|president of|"
                      r"open (now|late|today)|opening hours|hours|tickets?|availability|in stock|weather|forecast|"
                      r"flight status|traffic)\b", re.I)
LOCAL = re.compile(r"\b(near me|nearby|near \w+|around here|in my (area|city)|open late)\b", re.I)
# word stems on purpose ("vaccin" -> vaccine/vaccines/vaccination); endings pinned where a
# stem would also match everyday words (interactive, investigate, taxi)
HIGH_STAKES = re.compile(r"\b(dos(e|es|age|ing)\b|\d+\s?mg\b|medicat|medicine|drugs?\b|interactions?\b|side effects?\b|"
                         r"symptom|diagnos|pregnan|allerg|ibuprofen|paracetamol|acetaminophen|antibiotic|vaccin|"
                         r"overdose|lawsuit|sue\b|legal|illegal|visas?\b|immigra|tax(es)?\b|irs\b|gst\b|"
                         r"invest(ing|ment|ments|or|ors)?\b|mortgage|loans?\b|insurance claim|contract clause|"
                         r"tenant rights|evict)", re.I)
URL = re.compile(r"https?://\S+")
NO = re.compile(r"^\s*(write|draft|compose|rewrite|rephrase|translate|summari[sz]e (this|the|my)|proofread|"
                r"fix (the )?grammar|make (it|this) (shorter|longer)|what('s| is) \d|calculate|convert \d|"
                r"tell me a (joke|story)|(hi|hey|hello|thanks|thank you)\b)", re.I)
WEATHER = re.compile(r"\b(weather|forecast|temperature|rain(ing)?|snow(ing)?|umbrella|humid)\b", re.I)


@dataclass
class Hint:
    search: bool = False          # nudge the model to search first
    p: float | None = None        # Jev probability, when asked
    recency_days: int | None = None
    high_stakes: bool = False
    weather: bool = False
    read_url: str = ""
    reason: str = ""
    by: str = "rules"


def rules(text: str) -> Hint | None:
    """A decision from rules alone, or None when Jev should weigh in."""
    t = (text or "").strip()
    if not t:
        return Hint()
    url = URL.search(t)
    if url:
        return Hint(search=True, read_url=url.group(0).rstrip(").,"), reason="the user gave a link")
    stakes = bool(HIGH_STAKES.search(t))
    if WEATHER.search(t) and not stakes:
        return Hint(search=True, weather=True, recency_days=1, reason="weather")
    if NO.search(t) and not stakes and not EXPLICIT.search(t):
        return Hint(reason="writing, maths or small talk")
    if EXPLICIT.search(t):
        return Hint(search=True, high_stakes=stakes, recency_days=_recency(t), reason="the user asked to search")
    if stakes:
        return Hint(search=True, high_stakes=True, recency_days=_recency(t) or 730,
                    reason="health, legal or money question: verify and cite")
    if (TIME.search(t) and VOLATILE.search(t)) or LOCAL.search(t):
        return Hint(search=True, recency_days=_recency(t), reason="time-sensitive or local")
    return None


def _recency(t: str) -> int | None:
    if re.search(r"\b(today|tonight|right now|breaking|last night|yesterday|score|who won|live)\b", t, re.I):
        return 2
    if re.search(r"\b(this week|this weekend|last week|news|latest)\b", t, re.I):
        return 7
    if re.search(r"\b(price|prices|deal|discount|in stock|availability|tickets?)\b", t, re.I):
        return 30
    if re.search(r"\b(this month|recent(ly)?|upcoming|release|launch|version|update)\b", t, re.I):
        return 90
    if re.search(r"\b(this year|20(2[5-9]|3\d))\b", t, re.I):
        return 365
    return None


def decide(text: str, *, jev=None, today: str = "", timeout: float = 0.9) -> Hint:
    hint = rules(text)
    if hint is not None:
        return hint
    if jev is None or not getattr(jev, "available", False):
        return Hint(reason="no strong signal; the model decides")
    p = jev.noul({"user_message": text[:2000], "today": today or "unknown",
                  "assistant_knowledge": "general knowledge with a training cutoff about a year before today"},
                 "Answering this well needs up-to-date, local, niche or high-stakes information from the web "
                 "rather than general knowledge", timeout=timeout)
    if p is None:
        return Hint(reason="router unavailable; the model decides")
    return Hint(search=p >= 0.6, p=round(p, 2), recency_days=_recency(text), by="jev",
                reason=f"router confidence {p:.2f}")


def note_for(h: Hint) -> str:
    """Trusted runtime note for the first model step, or '' for none."""
    if not h.search:
        return ""
    if h.read_url:
        return (f"[Runtime note — trusted] The user shared a link ({h.read_url}). Read it with web.read (or the "
                f"browser if it needs interaction) before answering, and cite it.")
    if h.weather:
        return "[Runtime note — trusted] Weather question: use web.weather (not web.search) for the place asked about."
    rec = f" with recency_days≈{h.recency_days}" if h.recency_days else ""
    if h.high_stakes:
        return ("[Runtime note — trusted] This is a health, legal or money question. Check current, authoritative "
                f"sources with web.search{rec} before answering, cite them as [n], and say where people should "
                "confirm with a professional.")
    return (f"[Runtime note — trusted] This likely needs current or specific information from the web "
            f"({h.reason}). Call web.search{rec} before answering and cite sources as [n].")
