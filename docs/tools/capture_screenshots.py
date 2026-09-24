"""
Capture real screenshots of OpenMuse for the README and the launch video.

Runs against the live app (serve_nim.py + serve_ui on :8080) with the real
model, as a fresh demo account, so no personal chats or names appear.

    python docs/tools/capture_screenshots.py            # all shots
    python docs/tools/capture_screenshots.py search     # only shots whose name contains "search"

Writes PNGs to docs/screenshots/. Each shot is a real run: answers, sources,
cards and the live browser are whatever the model and the web return today.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "docs", "screenshots")
UI = os.environ.get("OPENMUSE_UI", "http://localhost:8080")
API = os.environ.get("OPENMUSE_API", "http://127.0.0.1:8765")
ONLY = sys.argv[1] if len(sys.argv) > 1 else ""


def api(method, path, body=None, token=""):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def main():
    os.makedirs(OUT, exist_ok=True)
    email = f"alex+{int(time.time())}@openmuse.demo"
    acct = api("POST", "/v1/auth/signup", {"email": email, "password": "openmuse-demo-2026", "name": "Alex Rivera",
                                            "timezone": "America/Los_Angeles"})
    token = acct["token"]
    log("demo account", email)
    # a little context so Goals / Monitors / Memory aren't empty
    api("POST", "/v1/goals", {"title": "Run a half marathon", "target_date": "2027-03-15",
                              "milestones": ["Run 5 km without stopping", "Run 10 km", "Run 15 km", "Race day"]}, token)
    api("POST", "/v1/goals", {"title": "Learn conversational Japanese", "target_date": "2027-06-01",
                              "milestones": ["Hiragana and katakana", "100 everyday phrases", "First 10-minute conversation"]}, token)
    try:
        api("POST", "/v1/monitors", {"url": "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html",
                                     "kind": "price_below", "target": "60", "every_minutes": 360}, token)
    except Exception as exc:
        log("monitor seed skipped:", exc)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])

        def page_for(width=1480, height=760, mobile=False):
            ctx = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=2,
                                      is_mobile=mobile, has_touch=mobile, color_scheme="light")
            ctx.grant_permissions(["microphone", "notifications"], origin=UI)
            ctx.add_init_script(f"try {{ localStorage.setItem('om.token', {json.dumps(token)}); }} catch (e) {{}}")
            p = ctx.new_page()
            p.set_default_timeout(20000)
            p.goto(UI + "/")
            p.wait_for_selector("textarea[aria-label='Message']")
            return p

        def new_chat(p):
            p.click("button[aria-label='New chat']")
            p.wait_for_timeout(500)

        def ask(p, text, wait=True, timeout=240):
            p.fill("textarea[aria-label='Message']", text)
            p.keyboard.press("Enter")
            p.wait_for_selector(".mc-send.stop", timeout=20000)
            if wait:
                t0 = time.time()
                while time.time() - t0 < timeout:
                    if not p.query_selector(".mc-send.stop") and not p.query_selector(".mc-bubble.streaming"):
                        break
                    p.wait_for_timeout(500)
                p.wait_for_timeout(1200)

        def shot(p, name, full=False):
            if ONLY and ONLY not in name:
                return
            path = os.path.join(OUT, name + ".png")
            p.screenshot(path=path, full_page=full)
            log("saved", name)

        def want(*names):
            return not ONLY or any(ONLY in n for n in names)

        p = page_for()

        # memory first, so later answers and the Memory page have something to show
        if want("memory"):
            new_chat(p)
            ask(p, "Please remember: I'm vegetarian, I live in San Francisco, and my sister Maya's birthday is March 3.")
            p.wait_for_timeout(8000)   # memory extraction runs in the background after the turn

        if want("search", "sources"):
            new_chat(p)
            ask(p, "What are 3 things to do in Kyoto in November? One line each.")
            p.evaluate("() => { const u = [...document.querySelectorAll('.mc-bubble.user')].pop(); if (u) u.scrollIntoView({block: 'start'}); }")
            p.evaluate("() => { const s = document.querySelector('.mc-scroll'); if (s) s.scrollTop -= 200; }")
            p.evaluate("() => { const l = document.querySelector('.mc-latest'); if (l) l.hidden = true; }")
            p.wait_for_timeout(400)
            shot(p, "01-web-search")
            btn = p.query_selector(".mc-srcchip-all")
            if btn:
                btn.click()
                p.wait_for_selector(".src-sheet")
                p.wait_for_timeout(600)
                shot(p, "02-sources")
                p.keyboard.press("Escape")
                p.wait_for_timeout(300)

        if want("streaming"):
            new_chat(p)
            ask(p, "Explain in about 180 words how solar panels turn sunlight into electricity.", wait=False)
            p.wait_for_function("(document.querySelector('.mc-bubble.streaming') || {innerText: ''}).innerText.length > 280",
                                timeout=90000)
            shot(p, "03-streaming")
            p.wait_for_function("!document.querySelector('.mc-send.stop')", timeout=120000)

        if want("weather"):
            new_chat(p)
            ask(p, "What's the weather in Tokyo this week? Just a quick summary.")
            p.evaluate("() => { const c = document.querySelector('.mc-wx-now'); if (c) c.closest('.mc-card').scrollIntoView({block: 'start'}); }")
            p.evaluate("() => { const s = document.querySelector('.mc-scroll'); if (s) s.scrollTop -= 150; }")
            p.wait_for_timeout(400)
            shot(p, "04-weather")

        if want("computer"):
            new_chat(p)
            ask(p, "Use the browser: go to books.toscrape.com, open the Travel category, and tell me the cheapest "
                   "book there and its price.", wait=False)
            p.wait_for_selector(".mc-tool.browser", timeout=90000)
            p.wait_for_timeout(2500)
            opener = p.query_selector(".mc-pill:not([hidden])") or p.query_selector(".mc-tool.browser")
            if opener:
                opener.click()
                p.wait_for_selector(".bv-modal", timeout=15000)
                # wait until the agent has moved past the first page, while it's still working
                t0 = time.time()
                while time.time() - t0 < 60:
                    log_items = p.eval_on_selector_all(".bv-log li", "els => els.length")
                    ended = "Session ended" in (p.inner_text(".bv-mode") if p.query_selector(".bv-mode") else "")
                    if log_items >= 4 or ended:
                        break
                    p.wait_for_timeout(700)
                p.wait_for_timeout(1200)
                shot(p, "05-computer-use")
                p.keyboard.press("Escape")
            p.wait_for_function("!document.querySelector('.mc-send.stop')", timeout=240000)
            p.wait_for_timeout(1500)
            shot(p, "06-computer-use-answer")

        if want("approval"):
            new_chat(p)
            ask(p, "Run the shell command `ls -la` in my workspace.", wait=False)
            p.wait_for_selector(".mc-approval", timeout=120000)
            p.wait_for_timeout(1200)
            shot(p, "07-approval")
            deny = p.query_selector(".mc-approval button:has-text('Deny')")
            if deny:
                deny.click()
            p.wait_for_function("!document.querySelector('.mc-send.stop')", timeout=120000)
            p.wait_for_timeout(1000)

        if want("memory"):
            p.click("#tabbar button[data-tab='chat']") if p.query_selector("#tabbar button[data-tab='chat']") else None
            p.click("#menuBtn"); p.wait_for_timeout(400)
            p.click("#drawer [data-tab='memory']"); p.wait_for_timeout(2500)
            shot(p, "08-memory")

        for tab, name in [("goals", "09-goals"), ("monitors", "10-monitors"), ("connectors", "11-apps")]:
            if want(name):
                p.click("#menuBtn"); p.wait_for_timeout(400)
                p.click(f"#drawer [data-tab='{tab}']"); p.wait_for_timeout(3000)
                shot(p, name)

        if want("library"):
            p.click("#menuBtn"); p.wait_for_timeout(400)
            p.click("#drawer [data-tab='chat']"); p.wait_for_timeout(400)
            new_chat(p)
            ask(p, "Make a short packing checklist for a 3-day trip to Kyoto in November and save it to my Library as a PDF.")
            p.click("#menuBtn"); p.wait_for_timeout(400)
            p.click("#drawer [data-tab='library']"); p.wait_for_timeout(2500)
            shot(p, "12-library")

        if want("voice"):
            import base64
            sys.path.insert(0, ROOT)
            env = dict(l.strip().split("=", 1) for l in open(os.path.join(ROOT, ".env")) if "=" in l and not l.startswith("#"))
            from api.voice import VoiceService
            question = VoiceService(nim_key=env["NVIDIA_NIM_API_KEY"]).speak(
                "What's a quick dinner I could make tonight?")[0]
            clip = base64.b64encode(question).decode()
            vctx = browser.new_context(viewport={"width": 1480, "height": 760}, device_scale_factor=2)
            vctx.grant_permissions(["microphone"], origin=UI)
            vctx.add_init_script(f"try {{ localStorage.setItem('om.token', {json.dumps(token)}); }} catch (e) {{}}")
            vctx.add_init_script("""(() => { window.__mic = {};
              navigator.mediaDevices.getUserMedia = async () => { const ctx = new AudioContext();
                const dest = ctx.createMediaStreamDestination(); window.__mic.ctx = ctx; window.__mic.dest = dest; return dest.stream; };
              window.__mic.play = async (b64) => { const ctx = window.__mic.ctx;
                const buf = await ctx.decodeAudioData(Uint8Array.from(atob(b64), c => c.charCodeAt(0)).buffer);
                const src = ctx.createBufferSource(); src.buffer = buf; src.connect(window.__mic.dest); src.start(); }; })();""")
            v = vctx.new_page()
            v.set_default_timeout(20000)
            v.goto(UI + "/")
            v.wait_for_selector("textarea[aria-label='Message']")
            new_chat(v)
            v.click("button[aria-label='Voice mode']")
            v.wait_for_selector(".vm[data-state='listening']", timeout=15000)
            v.wait_for_timeout(800)
            v.evaluate("(c) => __mic.play(c)", clip)
            v.wait_for_function("document.querySelector('.vm-reply') && document.querySelector('.vm-reply').innerText.length > 60",
                                timeout=90000)
            v.wait_for_timeout(1500)
            shot(v, "13-voice-mode")
            vctx.close()

        if want("mobile"):
            m = page_for(390, 844, mobile=True)
            new_chat(m)
            ask(m, "Is it going to rain in San Francisco this weekend?")
            m.evaluate("() => { const u = [...document.querySelectorAll('.mc-bubble.user')].pop(); if (u) u.scrollIntoView({block: 'start'}); }")
            m.evaluate("() => { const s = document.querySelector('.mc-scroll'); if (s) s.scrollTop -= 190; }")
            m.evaluate("() => { const l = document.querySelector('.mc-latest'); if (l) l.hidden = true; }")
            m.wait_for_timeout(400)
            shot(m, "14-mobile-chat")
            m.click("#menuBtn"); m.wait_for_timeout(500)
            shot(m, "15-mobile-menu")
        browser.close()


if __name__ == "__main__":
    main()
