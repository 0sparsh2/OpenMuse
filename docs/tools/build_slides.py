"""
Turn raw screenshots (docs/screenshots/*.png) into 1920x1080 presentation
slides for the README and the launch video (docs/slides/*.png).

    python docs/tools/build_slides.py

A hero slide (headline + phone), then one framed slide per feature: the real
screenshot inside a browser (or phone) frame on the OpenMuse gradient, with a
short title and a numbered label.
"""
from __future__ import annotations

import html
import os
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHOTS = os.path.join(ROOT, "docs", "screenshots")
OUT = os.path.join(ROOT, "docs", "slides")

LOGO = ('<svg viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="16" r="6" fill="#1d4ed8"/>'
        '<ellipse cx="16" cy="16" rx="13" ry="6" fill="none" stroke="#1d4ed8" stroke-width="2" transform="rotate(-24 16 16)"/>'
        '<circle cx="27" cy="10" r="2.4" fill="#1d4ed8"/></svg>')

# (file, screenshot, kind, title, subtitle, label)
SLIDES = [
    ("01-web-search", "01-web-search", "desktop", "Search with sources you can click",
     "It reads the pages, not just the snippets — every fact links back.", "WEB SEARCH"),
    ("02-computer-use", "05-computer-use", "desktop", "Watch it use a real browser",
     "Every click, live. Take control whenever you want.", "COMPUTER USE"),
    ("03-approval", "07-approval", "desktop", "It asks before it acts",
     "Sending, buying, signing in and shell commands wait for your OK.", "APPROVALS"),
    ("04-streaming", "03-streaming", "desktop", "Answers stream in as they're written",
     "Running on NVIDIA NIM — Nemotron for thinking, Riva for voice.", "STREAMING"),
    ("05-memory", "08-memory", "desktop", "It remembers what matters to you",
     "Your profile, people, and facts — yours to view and forget.", "MEMORY"),
    ("06-weather", "04-weather", "desktop", "Cards, not walls of text",
     "Weather, email, calendar, goals and documents as tidy cards.", "CARDS"),
    ("07-voice", "13-voice-mode", "desktop", "Talk to it. Interrupt it.",
     "Hands-free voice mode: talk over it to interrupt.", "VOICE"),
    ("08-mobile", "14-mobile-chat", "phone", "In your pocket, too",
     "An installable app with push notifications and share-to-OpenMuse.", "MOBILE"),
]

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:1920px;height:1080px;overflow:hidden}
body{font-family:-apple-system,"SF Pro Display","Inter","Segoe UI",system-ui,sans-serif;color:#131722;
  background:
    radial-gradient(1200px 800px at 0% 0%, #fbf6ec 0%, rgba(251,246,236,0) 60%),
    radial-gradient(1100px 900px at 100% 100%, #d7e8fb 0%, rgba(215,232,251,0) 62%),
    radial-gradient(900px 700px at 70% 10%, #ece9fb 0%, rgba(236,233,251,0) 60%),
    #f4f3f9;}
.brand{position:absolute;left:84px;top:62px;display:flex;align-items:center;gap:16px;font-weight:700;font-size:40px;letter-spacing:-.02em}
.brand svg{width:54px;height:54px}
.mascot{width:64px;height:64px;border-radius:50%;background:#f6e7da center/cover;box-shadow:0 6px 18px rgba(120,70,40,.18)}
.foot{position:absolute;left:84px;bottom:44px;font-size:24px;color:#5d6475}
.label{position:absolute;right:84px;bottom:44px;font-size:22px;font-weight:700;letter-spacing:.08em;color:#1d4ed8}
.head{position:absolute;right:84px;top:66px;text-align:right;max-width:980px}
.head h2{font-size:40px;letter-spacing:-.02em;font-weight:700}
.head p{margin-top:8px;font-size:23px;color:#4f5667}
.window{position:absolute;left:190px;right:190px;top:160px;bottom:96px;background:#fff;border-radius:26px;
  box-shadow:0 40px 90px rgba(35,45,90,.16),0 6px 20px rgba(35,45,90,.08);overflow:hidden;display:flex;flex-direction:column}
.bar{height:50px;flex:none;display:flex;align-items:center;gap:10px;padding:0 22px;background:#f7f7fa;border-bottom:1px solid #ebebf1;position:relative}
.dot{width:13px;height:13px;border-radius:50%}
.url{position:absolute;left:50%;transform:translateX(-50%);background:#ececf2;border-radius:10px;padding:7px 120px;font-size:18px;color:#626979}
.shot{flex:1;min-height:0;background:#fff center top/cover no-repeat}
.phone{position:absolute;width:430px;height:900px;border-radius:64px;background:#fff;padding:14px;
  box-shadow:0 50px 110px rgba(35,45,90,.22),0 0 0 2px #e4e6ee inset}
.phone .screen{width:100%;height:100%;border-radius:52px;background:#f4f3f9;position:relative;overflow:hidden;display:flex;flex-direction:column}
.phone .status{height:58px;flex:none;display:flex;align-items:center;justify-content:space-between;padding:6px 34px 0 40px;font-size:19px;font-weight:600;color:#111}
.phone .status i{display:inline-block;width:27px;height:13px;border:2px solid #111;border-radius:4px;position:relative}
.phone .status i::after{content:"";position:absolute;inset:1px;right:5px;background:#111;border-radius:1px}
.phone .app{flex:1;min-height:0;background:#fff center top/cover no-repeat}
.phone .island{position:absolute;top:14px;left:50%;transform:translateX(-50%);width:120px;height:34px;border-radius:20px;background:#0b0b0f;z-index:2}
/* hero */
.eyebrow{position:absolute;left:128px;top:300px;font-size:24px;font-weight:700;letter-spacing:.1em;color:#1d4ed8}
.hero-h{position:absolute;left:120px;top:350px;font-size:132px;line-height:1.02;font-weight:800;letter-spacing:-.035em}
.hero-p{position:absolute;left:128px;top:660px;font-size:34px;color:#4f5667;max-width:880px;line-height:1.35}
.cta{position:absolute;left:128px;top:790px;display:flex;align-items:center;gap:20px;background:#c9ddfb;color:#0f1f44;
  border-radius:44px;padding:24px 42px;font-size:30px;font-weight:700}
.cta i{width:0;height:0;border-left:26px solid #0f1f44;border-top:16px solid transparent;border-bottom:16px solid transparent}
.dashes{position:absolute;left:128px;top:940px;display:flex;gap:14px}
.dashes b{width:38px;height:5px;border-radius:3px;background:#d3d8e4}.dashes b:first-child{background:#1d4ed8}
"""


def page(body: str) -> str:
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{body}</body></html>"


MASCOT = "file://" + os.path.join(ROOT, "client", "web", "img", "avatar.webp")


def brand() -> str:
    return (f"<div class='brand'><span class='mascot' style=\"background-image:url('{MASCOT}')\"></span>"
            "OpenMuse</div>")


def img_url(name: str) -> str:
    return "file://" + os.path.join(SHOTS, name + ".png")


def phone(shot: str, style: str) -> str:
    return (f"<div class='phone' style='{style}'><div class='screen'><div class='island'></div>"
            "<div class='status'><span>9:41</span><i></i></div>"
            f"<div class='app' style=\"background-image:url('{img_url(shot)}')\"></div></div></div>")


def feature(shot: str, kind: str, title: str, sub: str, label: str, n: int, total: int) -> str:
    head = f"<div class='head'><h2>{html.escape(title)}</h2><p>{html.escape(sub)}</p></div>"
    if kind == "phone":   # two phones: the answer, and the menu
        frame = (phone(shot, "left:calc(50% - 470px);top:190px;transform:scale(.9);transform-origin:top center") +
                 phone("15-mobile-menu", "left:calc(50% + 40px);top:190px;transform:scale(.9);transform-origin:top center"))
    else:
        frame = ("<div class='window'><div class='bar'><span class='dot' style='background:#f0716b'></span>"
                 "<span class='dot' style='background:#f4bf4f'></span><span class='dot' style='background:#61c554'></span>"
                 "<span class='url'>OpenMuse · localhost:8080</span></div>"
                 f"<div class='shot' style=\"background-image:url('{img_url(shot)}')\"></div></div>")
    return page(brand() + head + frame +
                "<div class='foot'>Open source · Runs on NVIDIA NIM</div>"
                f"<div class='label'>{html.escape(label)} &nbsp;/&nbsp; {n:02d}</div>")


def hero() -> str:
    phone_html = phone("14-mobile-chat", "right:190px;top:90px")
    return page(brand() +
                "<div class='eyebrow'>OPENMUSE · A PERSONAL AGENT ON NVIDIA NIM</div>"
                "<div class='hero-h'>Ask it.<br>Watch it get done.</div>"
                "<div class='hero-p'>It searches the web, uses a real browser, remembers you — and asks before it acts.</div>"
                "<div class='cta'><i></i>Watch the demo</div>"
                "<div class='dashes'>" + "<b></b>" * 7 + "</div>"
                "<div class='foot'>Open source · Runs on NVIDIA NIM</div>" + phone_html)


def main():
    from playwright.sync_api import sync_playwright
    os.makedirs(OUT, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="om-slides-")
    jobs = [("00-hero", hero())]
    for i, (name, shot, kind, title, sub, label) in enumerate(SLIDES, start=1):
        if os.path.exists(os.path.join(SHOTS, shot + ".png")):
            jobs.append((name, feature(shot, kind, title, sub, label, i + 1, len(SLIDES) + 1)))
        else:
            print("missing screenshot, skipped:", shot)
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        p = b.new_page(viewport={"width": 1920, "height": 1080})
        for name, doc in jobs:
            path = os.path.join(tmp, name + ".html")
            with open(path, "w") as fh:
                fh.write(doc)
            p.goto("file://" + path)
            p.wait_for_timeout(300)
            p.screenshot(path=os.path.join(OUT, name + ".png"))
            print("slide", name)
        b.close()


if __name__ == "__main__":
    main()
