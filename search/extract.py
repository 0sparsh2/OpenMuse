"""Readable text (+ title and date) from fetched HTML, PDF or plain text."""
from __future__ import annotations

import io
import re


def extract(body: bytes, content_type: str, url: str) -> dict:
    ctype = (content_type or "").lower()
    if "pdf" in ctype or body[:5] == b"%PDF-":
        return _pdf(body)
    text = body.decode(_charset(ctype) or "utf-8", errors="replace")
    if "html" not in ctype and not re.search(r"<(html|body|p|div)\b", text[:4000], re.I):
        return {"title": "", "date": "", "text": _clean(text)}
    return _html(text, url)


def _charset(ctype: str) -> str:
    m = re.search(r"charset=([\w-]+)", ctype)
    return m.group(1) if m else ""


def _html(html: str, url: str) -> dict:
    title, date, text = "", "", ""
    try:
        import trafilatura
        text = trafilatura.extract(html, url=url, include_comments=False, include_tables=True,
                                   favor_precision=True) or ""
        meta = trafilatura.extract_metadata(html, default_url=url)
        if meta is not None:
            title, date = meta.title or "", meta.date or ""
    except Exception:
        text = ""
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    if len(text) < 200:  # extractor found nothing useful: fall back to visible text
        stripped = re.sub(r"<(script|style|noscript|svg|nav|footer|header)\b.*?</\1>", " ", html, flags=re.I | re.S)
        stripped = re.sub(r"<[^>]+>", " ", stripped)
        import html as _h
        text = _h.unescape(stripped)
    return {"title": _clean(title)[:200], "date": (date or "")[:10], "text": _clean(text)}


def _pdf(body: bytes) -> dict:
    try:
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(body))
        pages = [p.extract_text() or "" for p in r.pages[:30]]
        title = ((r.metadata or {}).get("/Title") or "") if r.metadata else ""
        return {"title": str(title)[:200], "date": "", "text": _clean("\n".join(pages))}
    except Exception:
        return {"title": "", "date": "", "text": ""}


def _clean(t: str) -> str:
    t = re.sub(r"[ \t ]+", " ", t or "")
    t = re.sub(r"\n\s*\n+", "\n\n", t)
    return t.strip()


def chunks(text: str, size: int = 800, overlap: int = 120) -> list[str]:
    """Paragraph-aware ~size-char passages."""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?=[A-Z0-9•\-])", text or "") if len(p.strip()) > 30]
    out, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 1 <= size:
            cur = (cur + "\n" + p).strip()
            continue
        if cur:
            out.append(cur)
        while len(p) > size:
            cut = p.rfind(". ", 0, size)
            cut = cut + 1 if cut > size // 2 else size
            out.append(p[:cut].strip())
            p = p[max(0, cut - overlap):].strip()
        cur = p
    if cur:
        out.append(cur)
    return out
