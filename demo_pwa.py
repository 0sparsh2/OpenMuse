"""
Installable app checks (issue #17): web push (real VAPID + aes128gcm, verified
by decrypting), and real Chromium against the UI server — service worker,
offline shell, manifest/icons, share target, push display, deep links, and
the phone layout.
"""
from __future__ import annotations

import base64
import http.server
import json
import os
import struct
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.environ.pop("NVIDIA_NIM_API_KEY", None)
os.environ["OPENMUSE_PUSH_ANY_HOST"] = "1"

from api import ApiBackend                 # noqa: E402
from api.push import PushService, valid_endpoint  # noqa: E402
from api.server import serve               # noqa: E402
from client.serve_ui import serve_ui       # noqa: E402

RESULTS: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def call(method, url, body=None, token=""):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def fake_sub(n: str) -> dict:
    return {"endpoint": f"https://push.example.test/{n}", "keys": {"p256dh": "BP" + "a" * 85, "auth": "x" * 22}}


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="openmuse-pwa-")
    SENT: list = []
    STATUS: dict = {}

    def sender(sub, data):
        SENT.append((sub["endpoint"], json.loads(data)))
        return STATUS.get(sub["endpoint"], 201)

    backend = ApiBackend(workspace_root=os.path.join(tmp, "ws"), db_path=os.path.join(tmp, "db.sqlite"),
                         accounts_root=os.path.join(tmp, "acct"), library_root=os.path.join(tmp, "users"),
                         push_key_file=os.path.join(tmp, "vapid.json"), push_sender=sender)
    push = backend.push

    # -- subscriptions + fan-out ------------------------------------------------------
    check("VAPID key file created private (0600)", oct(os.stat(os.path.join(tmp, "vapid.json")).st_mode)[-3:] == "600")
    check("VAPID key is stable across restarts",
          PushService(backend, key_file=os.path.join(tmp, "vapid.json"), sender=sender).public_key == push.public_key)
    backend._note_listeners.pop()  # drop the extra instance's listener
    check("public key is a P-256 point", len(unb64u(push.public_key)) == 65 and unb64u(push.public_key)[0] == 4)
    os.environ.pop("OPENMUSE_PUSH_ANY_HOST")
    check("only https push services are accepted",
          not valid_endpoint("http://fcm.googleapis.com/x") and not valid_endpoint("https://evil.example.com/x")
          and valid_endpoint("https://fcm.googleapis.com/fcm/send/abc") and valid_endpoint("https://web.push.apple.com/Q"))
    os.environ["OPENMUSE_PUSH_ANY_HOST"] = "1"
    try:
        push.subscribe("usr_a", {"endpoint": "https://push.example.test/k", "keys": {}}); ok = False
    except ValueError:
        ok = True
    check("subscription without keys rejected", ok)
    push.subscribe("usr_a", fake_sub("phone"), device="iPhone/iPad")
    push.subscribe("usr_a", fake_sub("laptop"), device="Mac")
    push.subscribe("usr_b", fake_sub("b-phone"))
    note = backend.notify("usr_a", kind="approval", title="OpenMuse needs your OK", body="Send the email to Sam? " * 30,
                          link={"chat_id": "c1"})
    time.sleep(0.5)
    got = [e for e, _ in SENT]
    check("a notification buzzes every device of that user", sorted(got) == ["https://push.example.test/laptop",
                                                                             "https://push.example.test/phone"], str(got))
    p = SENT[0][1]
    check("push payload: title, short body, deep link to the note",
          p["title"] == "OpenMuse needs your OK" and len(p["body"]) <= 200 and p["note_id"] == note["id"]
          and p["url"] == "/?note=" + note["id"] and p["kind"] == "approval")
    check("other users' devices get nothing", "https://push.example.test/b-phone" not in got)
    STATUS["https://push.example.test/laptop"] = 410
    SENT.clear()
    out = push.send("usr_a", {"title": "t"})
    check("a device that unsubscribed (410) is dropped", out == {"sent": 1, "dropped": 1, "failed": 0}
          and [s["device"] for s in push.subscriptions("usr_a")] == ["iPhone/iPad"])
    check("B can't remove A's device", not push.unsubscribe("usr_b", "https://push.example.test/phone"))

    # -- the real sender: VAPID JWT + aes128gcm, decrypted like a browser would --------
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    import http_ece
    ua_key = ec.generate_private_key(ec.SECP256R1())
    ua_pub = ua_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    CAP = {}

    class PushSvc(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            CAP["headers"] = dict(self.headers)
            CAP["body"] = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            CAP["path"] = self.path
            self.send_response(201); self.send_header("Content-Length", "0"); self.end_headers()

        def log_message(self, *a):
            pass
    svc = http.server.ThreadingHTTPServer(("127.0.0.1", 0), PushSvc)
    threading.Thread(target=svc.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{svc.server_address[1]}/push/abc"
    status = push._webpush({"endpoint": endpoint, "keys": {"p256dh": b64u(ua_pub), "auth": b64u(auth)}},
                           json.dumps({"title": "Approval needed", "note_id": "ntf_1"}))
    h = {k.lower(): v for k, v in CAP.get("headers", {}).items()}
    check("real web push delivered (201)", status == 201, str(status))
    check("payload is encrypted (aes128gcm), not plaintext",
          h.get("content-encoding") == "aes128gcm" and b"Approval needed" not in CAP["body"])
    try:
        plain = http_ece.decrypt(CAP["body"], private_key=ua_key, auth_secret=auth, version="aes128gcm")
        ok = json.loads(plain)["title"] == "Approval needed"
    except Exception as exc:
        ok = False
        print("   decrypt:", exc)
    check("the device can decrypt it (RFC 8291)", ok)
    authz = h.get("authorization", "")
    try:
        jwt = authz.split("t=")[1].split(",")[0].strip()
        k = authz.split("k=")[1].strip()
        head, claims, sig = jwt.split(".")
        claims = json.loads(unb64u(claims))
        raw = unb64u(sig)
        pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(push.public_key))
        pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                   f"{head}.{jwt.split('.')[1]}".encode(), ec.ECDSA(hashes.SHA256()))
        ok = k == push.public_key and claims["aud"] == f"http://127.0.0.1:{svc.server_address[1]}" \
            and claims["sub"].startswith("mailto:") and claims["exp"] > time.time()
    except Exception as exc:
        ok = False
        print("   vapid:", exc, authz[:80])
    check("VAPID JWT signed by our key, scoped to the push service", ok)
    check("urgent delivery with a TTL", h.get("urgency") == "high" and int(h.get("ttl", 0)) > 0)
    svc.shutdown()

    # -- real Chromium against the UI server -----------------------------------------------
    api_srv = serve(backend)
    api_base = f"http://127.0.0.1:{api_srv.server_address[1]}"
    ui = serve_ui(api_base, domains_root=os.path.join(tmp, "ui"), require_auth=True)
    base = f"http://localhost:{ui.server_address[1]}"   # localhost = secure context (SW + push allowed)
    st, res = call("POST", api_base + "/v1/auth/signup", {"email": "pwa@example.com", "password": "correct horse 42", "name": "Pat"})
    token, uid = res.get("token", ""), (res.get("user") or {}).get("user_id", "")
    check("signed up a test user", st in (200, 201) and token, str(res)[:200])

    req = urllib.request.Request(base + "/share", data=b"x=1", method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        urllib.request.build_opener(NoRedirect).open(req, timeout=5); loc = ""
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location", "") if e.code == 303 else f"code {e.code}"
    check("share without a service worker falls back to opening the app", loc == "/?share=unsupported", loc)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chromium")  # new headless: real notification support
        except Exception:
            browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 860})
        ctx.grant_permissions(["notifications"], origin=base)
        ctx.add_init_script(f"try {{ localStorage.setItem('om.token', {json.dumps(token)}); }} catch (e) {{}}")
        page = ctx.new_page()
        page.goto(base + "/")
        page.wait_for_function("navigator.serviceWorker && navigator.serviceWorker.controller", timeout=20000)
        check("service worker installed and controlling the app", True)

        man = page.evaluate("""async () => { const l = document.querySelector('link[rel=manifest]');
            const r = await fetch(l.href); return { ctype: r.headers.get('content-type'), m: await r.json() }; }""")
        m = man["m"]
        sizes = {(i["sizes"], i.get("purpose", "any")) for i in m["icons"]}
        check("manifest: standalone, start_url, theme, 192/512 + maskable icons",
              man["ctype"].startswith("application/manifest+json") and m["display"] == "standalone"
              and m["start_url"].startswith("/") and m["theme_color"] and
              {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes)
        ok = True
        for icon in m["icons"]:
            with urllib.request.urlopen(base + icon["src"]) as r:
                png = r.read()
            w, hgt = struct.unpack(">II", png[16:24])
            ok = ok and png[:8] == b"\x89PNG\r\n\x1a\n" and f"{w}x{hgt}" == icon["sizes"]
        check("every icon is a real PNG of its declared size", ok)
        check("share target accepts links, text and files",
              m["share_target"]["method"] == "POST" and m["share_target"]["params"]["files"][0]["name"] == "files")

        # offline: the app shell still opens
        ctx.set_offline(True)
        page.reload()
        shell = page.evaluate("() => !!document.querySelector('#main') && !!document.querySelector('#tabbar') && document.title")
        ctx.set_offline(False)
        check("app opens offline from the cached shell", shell == "OpenMuse", str(shell))
        page.goto(base + "/")
        page.wait_for_function("navigator.serviceWorker.controller")
        keys = page.evaluate("async () => { const out = []; for (const n of await caches.keys()) { const c = await caches.open(n); (await c.keys()).forEach((r) => out.push(new URL(r.url).pathname)); } return out; }")
        check("no API data is cached on the device", keys and not any(k.startswith("/v1") for k in keys), str(keys))

        # share target: a phone shares a link + a PDF into OpenMuse
        page.evaluate("""() => {
            const f = document.createElement('form'); f.method = 'POST'; f.action = '/share'; f.enctype = 'multipart/form-data';
            const t = document.createElement('input'); t.name = 'text'; t.value = 'Worth reading https://example.com/article'; f.appendChild(t);
            const i = document.createElement('input'); i.type = 'file'; i.name = 'files';
            const dt = new DataTransfer(); dt.items.add(new File(['%PDF-1.4\\n%%EOF'], 'boarding-pass.pdf', { type: 'application/pdf' }));
            i.files = dt.files; f.appendChild(i); document.body.appendChild(f); f.submit(); }""")
        page.wait_for_selector(".share-sheet", timeout=15000)
        what = page.inner_text(".share-sheet .share-what")
        check("shared content opens the share sheet", "https://example.com/article" in what and "boarding-pass.pdf" in what, what)
        check("URL cleaned after landing", page.evaluate("location.search") == "")
        page.click(".share-sheet >> text=Just save to Library")
        page.wait_for_selector(".share-sheet", state="detached", timeout=10000)
        docs = backend.library.list(uid)
        check("shared file saved to the user's Library", any(d["name"] == "boarding-pass.pdf" for d in docs),
              str([d["name"] for d in docs]))
        left = page.evaluate("async () => (await (await caches.open('om-share')).keys()).length")
        check("shared content isn't kept on the device afterwards", left == 0)

        # the push toggle is offered in the notification panel
        page.click("#bellBtn")
        page.wait_for_selector(".notif-panel")
        panel = page.inner_text(".notif-panel")
        check("notification panel offers push for this device", "Get notifications on this device" in panel, panel[:200])
        page.keyboard.press("Escape")
        page.mouse.click(5, 400)

        # a push arriving at the service worker shows a notification with the deep link
        call("POST", base + "/v1/push/subscribe", {"subscription": fake_sub("chromium")}, token)
        SENT.clear()
        n2 = backend.notify(uid, kind="approval", title="OpenMuse needs your OK", body="Book the 9:05 flight?")
        t0 = time.time()
        while not SENT and time.time() - t0 < 5:
            time.sleep(0.05)
        payload = SENT[0][1] if SENT else {}
        cdp = ctx.new_cdp_session(page)
        regs = []
        cdp.on("ServiceWorker.workerRegistrationUpdated", lambda e: regs.extend(e["registrations"]))
        cdp.send("ServiceWorker.enable")
        t0 = time.time()
        while not regs and time.time() - t0 < 5:
            page.wait_for_timeout(100)
        try:
            cdp.send("ServiceWorker.deliverPushMessage", {"origin": base + "/", "registrationId": regs[0]["registrationId"],
                                                          "data": json.dumps(payload)})
            page.wait_for_timeout(800)
            shown = page.evaluate("async () => (await (await navigator.serviceWorker.ready).getNotifications()).map((n) => ({ t: n.title, b: n.body, d: n.data, ri: n.requireInteraction }))")
        except Exception as exc:
            shown = []
            print("   push delivery:", exc)
        check("push shows a notification on the device", any(s["t"] == "OpenMuse needs your OK" and s["b"] == "Book the 9:05 flight?"
                                                              for s in shown), str(shown))
        check("approval notifications stay until handled and link to the card",
              any(s["ri"] and s["d"]["note_id"] == n2["id"] and s["d"]["url"] == "/?note=" + n2["id"] for s in shown))
        page.goto(base + "/?note=" + n2["id"])
        page.wait_for_function("location.search === ''", timeout=10000)
        t0 = time.time()
        while time.time() - t0 < 5 and not next((n for n in backend.notifications(uid) if n["id"] == n2["id"]), {}).get("read_at"):
            time.sleep(0.1)
        check("tapping the notification opens it (marked read)",
              next(n for n in backend.notifications(uid) if n["id"] == n2["id"])["read_at"])

        # phone layout
        phone = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                                    device_scale_factor=3)
        phone.add_init_script(f"try {{ localStorage.setItem('om.token', {json.dumps(token)}); }} catch (e) {{}}")
        pp = phone.new_page()
        pp.goto(base + "/")
        pp.wait_for_selector("#tabbar button")
        lay = pp.evaluate("""() => ({ sw: document.documentElement.scrollWidth,
            tabs: Array.from(document.querySelectorAll('#tabbar button')).map((b) => b.getBoundingClientRect().height),
            top: Array.from(document.querySelectorAll('.top-btn')).map((b) => [b.getBoundingClientRect().width, b.getBoundingClientRect().height]),
            vp: document.querySelector('meta[name=viewport]').content })""")
        check("phone: no sideways scrolling at 390px", lay["sw"] <= 390, str(lay["sw"]))
        check("phone: tap targets at least 44px", all(h >= 44 for h in lay["tabs"]) and all(min(t) >= 44 for t in lay["top"]), str(lay))
        check("phone: content extends under the notch (safe areas handled)", "viewport-fit=cover" in lay["vp"])
        pp.click("#menuBtn")
        pp.wait_for_timeout(400)
        dr = pp.evaluate("""() => { const d = document.querySelector('#drawer'), n = d.querySelector('.nav');
            return { dir: getComputedStyle(d).flexDirection, nav: getComputedStyle(n).flexDirection,
                     side: d.scrollWidth > d.clientWidth + 1,
                     full: document.querySelector('#drawerNew').getBoundingClientRect().width > d.clientWidth * 0.8 } }""")
        check("phone: the menu stays a column (it used to collapse into a squashed strip)",
              dr["dir"] == "column" and dr["nav"] == "column" and not dr["side"] and dr["full"], str(dr))
        pp.keyboard.press("Escape")
        check("phone: no 'Latest messages' button over the start screen", not pp.is_visible(".mc-latest"))
        phone.close()
        browser.close()

    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
