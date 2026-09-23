"""
Voice mode checks (issue #19).

  1. Server: speech endpoints, voice runs (short-answer hint + low-latency
     model flag), and the rule that R3+ approvals can't be given by voice.
  2. Real providers (when NVIDIA_NIM_API_KEY is in .env): speech -> NVIDIA
     Riva ASR -> NIM model (voice settings) -> Riva TTS, timed end to end.
  3. Real Chromium with a scripted microphone: VAD, 16 kHz WAV upload,
     sentence-chunked speech, barge-in (<300 ms), voice approvals.
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import struct
import sys
import tempfile
import time
import urllib.request
import wave

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
ENV = {}
if os.path.exists(os.path.join(ROOT, ".env")):
    for line in open(os.path.join(ROOT, ".env")):
        m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
        if m:
            ENV[m.group(1)] = m.group(2)
os.environ.pop("NVIDIA_NIM_API_KEY", None)

from api import ApiBackend                              # noqa: E402
from api.server import serve                            # noqa: E402
from api.voice import VoiceService, speakable           # noqa: E402
from client.serve_ui import serve_ui                    # noqa: E402
from connectors.composio_bridge import ComposioBridge   # noqa: E402
from gateway import ModelResponse, ToolCall             # noqa: E402

RESULTS: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def tone_wav(seconds: float, rate: int = 22050, amp: float = 0.05) -> bytes:
    n = int(seconds * rate)
    pcm = b"".join(struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * 220 * i / rate))) for i in range(n))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm)
    return buf.getvalue()


def call(method, url, body=None, token="", raw=False):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
            return r.status, (data, r.headers.get("Content-Type")) if raw else json.loads(data or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# A microphone the test controls: silence until __mic.speak(ms) plays
# speech-like noise (amplitude-modulated, so noise suppression keeps it).
MIC_SCRIPT = """
(() => {
  const real = navigator.mediaDevices && navigator.mediaDevices.getUserMedia && navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  window.__mic = { gain: null, ctx: null, log: [] };
  navigator.mediaDevices.getUserMedia = async (c) => {
    if (!c || !c.audio) return real(c);
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const len = ctx.sampleRate * 2, buf = ctx.createBuffer(1, len, ctx.sampleRate), d = buf.getChannelData(0);
    for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * (0.6 + 0.4 * Math.sin(i / ctx.sampleRate * 2 * Math.PI * 4));
    const src = ctx.createBufferSource(); src.buffer = buf; src.loop = true;
    const g = ctx.createGain(); g.gain.value = 0;
    const dest = ctx.createMediaStreamDestination();
    src.connect(g); g.connect(dest); src.start();
    window.__mic.gain = g; window.__mic.ctx = ctx;
    return dest.stream;
  };
  window.__mic.speak = (ms) => {
    const g = window.__mic.gain; if (!g) return false;
    window.__mic.log.push({ start: performance.now(), ms });
    g.gain.setValueAtTime(0.35, g.context.currentTime);
    g.gain.setValueAtTime(0, g.context.currentTime + ms / 1000);
    return true;
  };
})();
"""


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="openmuse-voice-")
    STT_QUEUE: list[str] = []
    STT_SEEN: list[bytes] = []
    TTS_SEEN: list[str] = []
    SEEN_REQ: list = []
    REPLY = {"text": "It's sunny in Paris today, around twenty-four degrees. Enjoy your walk along the river this afternoon!"}

    def stt(wav):
        STT_SEEN.append(wav)
        return STT_QUEUE.pop(0) if STT_QUEUE else ""

    def tts(text):
        TTS_SEEN.append(text)
        time.sleep(0.05)
        return tone_wav(2.5), "audio/wav"

    def respond(request, history):
        SEEN_REQ.append(request)
        user = next((b.text for m in reversed(request.messages) if m.role == "user" for b in m.blocks
                     if b.trust == "user" and not b.text.startswith("[")), "")
        tools = [m for m in request.messages if m.role == "tool"]
        if "save" in user.lower() and not tools:
            return ModelResponse(text="", stop_reason="tool_calls", tool_calls=[ToolCall(
                id="w1", name="files.write", arguments={"path": "notes/voice.txt", "content": "hello"})])
        if "email" in user.lower() and not tools:
            return ModelResponse(text="", stop_reason="tool_calls", tool_calls=[ToolCall(
                id="e1", name="gmail.send_email", arguments={"recipient_email": "sam@example.com",
                                                             "subject": "Hi", "body": "Hello Sam"})])
        if tools:
            return ModelResponse(text="Done.", stop_reason="stop")
        return ModelResponse(text=REPLY["text"], stop_reason="stop")

    from demo_composio import FakeComposio
    fake = FakeComposio({})
    backend = ApiBackend(workspace_root=os.path.join(tmp, "ws"), respond=respond, db_path=os.path.join(tmp, "db.sqlite"),
                         accounts_root=os.path.join(tmp, "acct"), voice=VoiceService(stt=stt, tts=tts),
                         connectors=ComposioBridge("", cache_dir=tmp, client=fake))

    # -- 1. server -----------------------------------------------------------------------
    v = backend.voice
    check("config names the providers", v.config()["stt"] == "custom" and v.config()["tts"] == "custom")
    for bad, why in [(b"", "empty"), (b"OggS" + b"\0" * 100, "not wav"), (b"RIFF" + b"\0" * 4 + b"WAVE" + b"\0" * (9 * 1024 * 1024), "too long")]:
        try:
            v.transcribe(bad); ok = False
        except ValueError:
            ok = True
        check(f"transcribe rejects bad audio ({why})", ok)
    check("markdown, URLs and code aren't read aloud",
          speakable("**Done!** See [the page](https://x.com/a) or https://y.com/b\n- one\n```py\nx=1\n```")
          == "Done! See the page or the link on screen one")
    none = VoiceService()
    check("without providers, config says to use the browser", none.config()["stt"] is None and none.config()["tts"] is None)

    api_srv = serve(backend)
    api = f"http://127.0.0.1:{api_srv.server_address[1]}"
    st, res = call("POST", api + "/v1/auth/signup", {"email": "v@example.com", "password": "correct horse 42", "name": "Val"})
    token, uid = res.get("token", ""), (res.get("user") or {}).get("user_id", "")
    fake.connected[uid] = {"gmail"}
    st, cfg = call("GET", api + "/v1/voice/config", token=token)
    check("GET /v1/voice/config", st == 200 and cfg["stt"] == "custom")
    st, (audio, ctype) = call("POST", api + "/v1/voice/speak", {"text": "Hello there."}, token, raw=True)
    check("POST /v1/voice/speak returns playable audio", st == 200 and ctype == "audio/wav" and audio[:4] == b"RIFF")
    import base64
    STT_QUEUE[:] = ["hello from the api"]
    st, tr = call("POST", api + "/v1/voice/transcribe", {"audio_base64": base64.b64encode(tone_wav(0.5, 16000)).decode()}, token)
    check("POST /v1/voice/transcribe returns the text", st == 200 and tr["text"] == "hello from the api")
    st, _ = call("POST", api + "/v1/voice/speak", {"text": "hi"})
    check("speech endpoints need sign-in", st == 401)

    sess = backend.create_session(user_id=uid)
    SEEN_REQ.clear()
    run, _, _ = backend.submit_message(chat_id=sess.chat_id, user_id=uid, content=[{"type": "text", "text": "weather?"}], mode="voice")
    t0 = time.time()
    while run.state not in ("COMPLETED", "FAILED") and time.time() - t0 < 10:
        time.sleep(0.05)
    sys_text = SEEN_REQ[0].messages[0].blocks[0].text if SEEN_REQ else ""
    check("voice turns ask for short spoken answers", run.mode == "voice" and "VOICE MODE" in sys_text)
    check("voice turns are flagged for the low-latency model", SEEN_REQ and SEEN_REQ[0].metadata.mode == "voice")
    SEEN_REQ.clear()
    run2, _, _ = backend.submit_message(chat_id=sess.chat_id, user_id=uid, content=[{"type": "text", "text": "weather?"}], mode="shout")
    while run2.state not in ("COMPLETED", "FAILED"):
        time.sleep(0.05)
    check("typed turns (and unknown modes) are unaffected",
          run2.mode == "" and "VOICE MODE" not in SEEN_REQ[0].messages[0].blocks[0].text)

    def parked(text):
        r, _, _ = backend.submit_message(chat_id=sess.chat_id, user_id=uid, content=[{"type": "text", "text": text}], mode="voice")
        t0 = time.time()
        while r.state != "WAITING_FOR_APPROVAL" and time.time() - t0 < 10:
            time.sleep(0.05)
        return r, backend.approvals.requests[backend._pending_approval[r.run_id]]
    r3, req3 = parked("email Sam hello")
    st, body = call("POST", api + f"/v1/approvals/{req3.id}/decision",
                    {"decision": "approve", "argument_hash": req3.argument_hash, "channel": "voice"}, token)
    check("sending email can't be approved by voice (R3 → confirm on screen)",
          st == 403 and body["error"]["code"] == "CONFIRM_ON_SCREEN" and req3.status == "pending", f"{st} {body}")
    st, body = call("POST", api + f"/v1/approvals/{req3.id}/decision",
                    {"decision": "deny", "argument_hash": req3.argument_hash, "channel": "voice"}, token)
    check("…but you can always say no by voice", st == 200)
    r2, req2 = parked("save a note")
    st, body = call("POST", api + f"/v1/approvals/{req2.id}/decision",
                    {"decision": "approve", "argument_hash": req2.argument_hash, "channel": "voice"}, token)
    check("a low-risk step (R2) can be approved by voice", st == 200 and req2.risk == "R2", f"{st} {body}")

    # -- 2. real providers: speech -> ASR -> model -> TTS ---------------------------------
    key = ENV.get("NVIDIA_NIM_API_KEY", "")
    if key:
        real = VoiceService(nim_key=key)
        check("NVIDIA Riva speech is available with the NIM key", real.stt_name and real.tts_name,
              f"{real.stt_name} {real.tts_name}")
        q_audio, _ = real.speak("What's a good name for a golden retriever puppy?")
        with wave.open(io.BytesIO(q_audio)) as w:
            pcm, rate = w.readframes(w.getnframes()), w.getframerate()
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        step = rate / 16000
        down = [samples[int(i * step)] for i in range(int(len(samples) / step))]
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(struct.pack(f"<{len(down)}h", *down))
        t0 = time.time()
        heard = real.transcribe(buf.getvalue())["text"]
        t_stt = time.time() - t0
        check("real ASR understands a spoken question", "golden retriever" in heard.lower(), heard)
        from gateway.providers.openai_compat import OpenAICompatProvider
        from gateway.protocol import ModelRequest, RequestMetadata
        from gateway.protocol import ChatMessage, Block
        prov = OpenAICompatProvider(api_key=key, base_url=ENV.get("NVIDIA_NIM_API_BASE", "https://integrate.api.nvidia.com/v1"),
                                    model=ENV.get("NVIDIA_VOICE_MODEL", ENV.get("NVIDIA_MODEL", "")),
                                    extra_body={"chat_template_kwargs": {"enable_thinking": False}})
        hint = ("You are OpenMuse, a personal assistant.\n\nVOICE MODE: the user is talking to you and your reply is read "
                "aloud. Answer in one to three short, natural spoken sentences. No markdown, lists, tables, emoji or URLs.")
        req = ModelRequest(request_id="voice-probe", model_class="planner", max_output_tokens=200,
                           messages=[ChatMessage(role="system", blocks=[Block(kind="text", text=hint)]),
                                     ChatMessage(role="user", blocks=[Block(kind="text", text=heard, trust="user")])],
                           metadata=RequestMetadata(tenant_id="t", run_id="r", step=0, prompt_version="v", mode="voice"))
        # voice runs stream tokens: speech starts once the first sentence is written
        from gateway import streaming
        got = {"text": "", "first_at": None}

        def sink(delta, step):
            got["text"] += delta
            if got["first_at"] is None and re.search(r"[.!?][\"')\]]*\s", got["text"]):
                got["first_at"] = time.time()
        streaming.register("r", sink)
        t0 = time.time()
        try:
            answer = prov.complete(req).text.strip()
        except Exception as exc:
            answer = ""
            print("   model:", exc)
        streaming.unregister("r")
        t_all = time.time() - t0
        t_llm = (got["first_at"] or time.time()) - t0
        check("voice turns stream the model's words as they're written", got["text"].strip() == answer and got["first_at"])
        first = re.match(r"[^.!?]*[.!?]+", answer)
        first = first.group(0) if first else answer
        t0 = time.time()
        a2, _ = real.speak(first)
        t_tts = time.time() - t0
        total = t_stt + t_llm + t_tts
        print(f"   latency: ASR {t_stt:.2f}s + first sentence {t_llm:.2f}s (whole answer {t_all:.2f}s) "
              f"+ TTS {t_tts:.2f}s = {total:.2f}s  «{answer[:90]}»")
        check("spoken answer is short and plain", answer and len(answer) < 400 and not re.search(r"[*#`]|https?://", answer), answer)
        check("end of speech → first audio under 2.5s (real ASR + model + TTS)", total < 2.5, f"{total:.2f}s")
    else:
        print("   (no NVIDIA_NIM_API_KEY in .env — skipping real-provider checks)")

    # -- 3. real Chromium, scripted microphone ----------------------------------------------
    ui = serve_ui(api, domains_root=os.path.join(tmp, "ui"), require_auth=True)
    base = f"http://localhost:{ui.server_address[1]}"
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chromium", args=["--autoplay-policy=no-user-gesture-required"])
        except Exception:
            browser = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context()
        ctx.grant_permissions(["microphone"], origin=base)
        ctx.add_init_script(f"try {{ localStorage.setItem('om.token', {json.dumps(token)}); }} catch (e) {{}}")
        ctx.add_init_script(MIC_SCRIPT)
        page = ctx.new_page()
        page.goto(base + "/")
        page.wait_for_selector("button[aria-label='Voice mode']")
        page.click("button[aria-label='Voice mode']")
        page.wait_for_selector(".vm[data-state='listening']", timeout=10000)
        check("voice mode opens and listens", True)

        STT_QUEUE[:] = ["What's the weather in Paris?"]
        STT_SEEN.clear(); TTS_SEEN.clear()
        page.wait_for_timeout(600)                     # let the noise floor settle
        page.evaluate("__mic.speak(1300)")
        page.wait_for_selector(".vm[data-state='speaking']", timeout=15000)
        wav = STT_SEEN[0] if STT_SEEN else b""
        ok = wav[:4] == b"RIFF"
        if ok:
            with wave.open(io.BytesIO(wav)) as w:
                dur = w.getnframes() / w.getframerate()
                ok = w.getframerate() == 16000 and w.getnchannels() == 1 and 1.2 <= dur <= 3.0
        check("your speech is captured and sent as 16 kHz mono WAV", ok, f"{len(wav)} bytes")
        runs = [r for r in backend.runs._runs.values() if r.user_id == uid and backend._run_text.get(r.run_id) == "What's the weather in Paris?"]
        check("what you said is sent as a voice turn", runs and runs[-1].mode == "voice")
        page.wait_for_function("__omVoice.stats.latencies.length > 0", timeout=10000)
        lat = page.evaluate("__omVoice.stats.latencies[0]")
        check("first audio starts quickly after you stop talking (VAD + STT + model + TTS)", lat < 2500, f"{lat} ms")
        check("the answer is spoken sentence by sentence", len(TTS_SEEN) == 2 and TTS_SEEN[0].startswith("It's sunny")
              and TTS_SEEN[1].startswith("Enjoy"), str(TTS_SEEN))
        thread = page.evaluate("document.querySelector('.vm-reply').textContent")
        check("the reply text shows while it's spoken", "twenty-four degrees" in thread)

        # barge-in: talk over it
        STT_QUEUE[:] = ["Actually, what about London?"]
        page.wait_for_timeout(500)
        stop_ms = page.evaluate("""() => new Promise((resolve) => {
            const t0 = performance.now(); __mic.speak(1100);
            const tick = () => { const v = __omVoice; const p = v.playing;
              if (v.state !== 'speaking' && (!p || !p.el || p.el.paused)) resolve(Math.round(performance.now() - t0));
              else if (performance.now() - t0 > 3000) resolve(-1); else requestAnimationFrame(tick); };
            tick(); })""")
        check("talking over it stops the audio within 300 ms", 0 <= stop_ms <= 300, f"{stop_ms} ms")
        page.wait_for_function("__omVoice.stats.latencies.length > 1", timeout=15000)
        check("…and what you said next is captured as the new request",
              any(backend._run_text.get(r.run_id) == "Actually, what about London?" and r.mode == "voice"
                  for r in backend.runs._runs.values()))

        # approvals by voice
        page.wait_for_selector(".vm[data-state='listening']", timeout=15000)
        STT_QUEUE[:] = ["Please save a note for me"]
        page.evaluate("__mic.speak(1200)")
        page.wait_for_function("document.querySelector('.vm-reply').textContent.startsWith('Should I')", timeout=15000)
        asked = page.evaluate("document.querySelector('.vm-reply').textContent")
        check("a low-risk step is asked out loud", "save a file" in asked, asked)
        page.wait_for_selector(".vm[data-state='listening']", timeout=15000)
        STT_QUEUE[:] = ["yes please"]
        page.evaluate("__mic.speak(700)")
        t0 = time.time()
        saved = os.path.join(backend.memory.workspace_root(uid) if backend.memory else os.path.join(tmp, "ws"), "notes", "voice.txt")
        while not os.path.exists(saved) and time.time() - t0 < 15:
            time.sleep(0.1)
        check("saying “yes” approves it", os.path.exists(saved))

        page.wait_for_selector(".vm[data-state='listening']", timeout=15000)
        STT_QUEUE[:] = ["Email Sam to say hello"]
        page.evaluate("__mic.speak(1200)")
        page.wait_for_selector(".vm-acts button", timeout=15000)
        said = page.evaluate("document.querySelector('.vm-reply').textContent")
        check("sending email is sent to the screen, not approved by voice",
              said.startswith("I need your OK on screen") and page.inner_text(".vm-acts button") == "Review on screen", said)
        page.click(".vm-acts button")
        page.wait_for_selector(".vm", state="detached")
        check("“Review on screen” returns to the chat with the approval card",
              page.evaluate("!!document.querySelector('[data-view=chat]:not([hidden])')"))
        browser.close()

    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
