"""
Voice mode (issue #19): server speech-to-text and text-to-speech.

Providers, first match wins:
  - NVIDIA Riva on NIM (hosted gRPC, same NVIDIA_NIM_API_KEY as the model):
    ASR = Whisper large-v3 (or Parakeet), TTS = Magpie multilingual.
  - Any OpenAI-compatible /audio/transcriptions + /audio/speech endpoint
    (OPENMUSE_STT_BASE / OPENMUSE_TTS_BASE, *_KEY, *_MODEL, OPENMUSE_TTS_VOICE).
  - Neither: config says so and the client uses the browser's Web Speech /
    speechSynthesis instead.

The client uploads one utterance as 16 kHz mono WAV (encoded in the page, so
every browser produces the same format) and asks for speech a sentence at a
time as the answer streams, so the first audio starts after one sentence.
Audio isn't stored; transcripts only enter the chat as the user's message.
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import urllib.request
import uuid
import wave

MAX_AUDIO_BYTES = 8 * 1024 * 1024      # ~4 min of 16 kHz mono PCM
MAX_TTS_CHARS = 600                     # one or two sentences per call

RIVA_URI = "grpc.nvcf.nvidia.com:443"
RIVA_ASR = {"whisper": ("b702f636-f60c-4a3d-a6f4-f3568c13bd7d", "en"),
            "parakeet": ("1598d209-5e27-4d3c-8079-4751568b1081", "en-US")}
RIVA_TTS = ("877104f7-e885-42b9-8de8-f6e4c6303969", "en-US")  # magpie-tts-multilingual
TTS_RATE = 22050


def pcm_to_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def speakable(text: str) -> str:
    """Strip what shouldn't be read aloud (markdown, URLs, code)."""
    t = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"\s*(\[\d{1,3}(\s*,\s*\d{1,3})*\]|\[\d{1,3}†[^\]]*\]|【[^】]{0,80}】)", "", t)   # citation markers
    t = re.sub(r"https?://\S+", "the link on screen", t)
    t = re.sub(r"[*_#>|~]+", "", t)
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.M)
    return re.sub(r"\s+", " ", t).strip()[:MAX_TTS_CHARS]


class VoiceService:
    def __init__(self, *, nim_key: str = "", asr_model: str = "", stt=None, tts=None):
        self.nim_key = nim_key
        self.asr_model = asr_model or os.environ.get("OPENMUSE_ASR_MODEL", "whisper")
        self._stt_fn, self._tts_fn = stt, tts
        self.stt_name = self.tts_name = None
        self._riva_ok = False
        if nim_key and (stt is None or tts is None):
            try:
                import riva.client  # noqa: F401
                self._riva_ok = True
            except Exception:
                pass
        if stt is not None:
            self.stt_name = "custom"
        elif os.environ.get("OPENMUSE_STT_BASE"):
            self.stt_name, self._stt_fn = "openai-compatible", self._stt_openai
        elif self._riva_ok:
            self.stt_name, self._stt_fn = f"nvidia-riva-{self.asr_model}", self._stt_riva
        if tts is not None:
            self.tts_name = "custom"
        elif os.environ.get("OPENMUSE_TTS_BASE"):
            self.tts_name, self._tts_fn = "openai-compatible", self._tts_openai
        elif self._riva_ok:
            self.tts_name, self._tts_fn = "nvidia-riva-magpie", self._tts_riva
        self._clients: dict = {}
        self._lock = threading.Lock()

    def config(self) -> dict:
        return {"stt": self.stt_name, "tts": self.tts_name, "sample_rate": 16000,
                "max_audio_seconds": MAX_AUDIO_BYTES // 32000}

    # -- public -----------------------------------------------------------------------
    def transcribe(self, wav: bytes) -> dict:
        if self._stt_fn is None:
            raise LookupError("no speech-to-text provider; use the browser's")
        if not wav or len(wav) > MAX_AUDIO_BYTES:
            raise ValueError("audio missing or too long")
        if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
            raise ValueError("audio must be WAV (16 kHz mono PCM)")
        t0 = time.time()
        text = (self._stt_fn(wav) or "").strip()
        return {"text": text, "ms": int((time.time() - t0) * 1000), "provider": self.stt_name}

    def speak(self, text: str) -> tuple[bytes, str]:
        """(audio bytes, content type)."""
        if self._tts_fn is None:
            raise LookupError("no text-to-speech provider; use the browser's")
        t = speakable(text)
        if not t:
            raise ValueError("nothing to say")
        return self._tts_fn(t)

    # -- NVIDIA Riva (NIM hosted) -------------------------------------------------------
    def _riva(self, kind: str, fid: str):
        with self._lock:
            if (kind, fid) not in self._clients:
                import riva.client
                auth = riva.client.Auth(uri=RIVA_URI, use_ssl=True, metadata_args=[
                    ["function-id", fid], ["authorization", "Bearer " + self.nim_key]])
                self._clients[(kind, fid)] = (riva.client.ASRService(auth) if kind == "asr"
                                              else riva.client.SpeechSynthesisService(auth))
            return self._clients[(kind, fid)]

    def _stt_riva(self, wav: bytes) -> str:
        import riva.client
        fid, lang = RIVA_ASR.get(self.asr_model, RIVA_ASR["whisper"])
        cfg = riva.client.RecognitionConfig(language_code=lang, max_alternatives=1,
                                            enable_automatic_punctuation=True)
        r = self._riva("asr", fid).offline_recognize(wav, cfg)
        return " ".join(x.alternatives[0].transcript.strip() for x in r.results if x.alternatives)

    def _tts_riva(self, text: str) -> tuple[bytes, str]:
        fid, lang = RIVA_TTS
        r = self._riva("tts", fid).synthesize(text, voice_name=os.environ.get("OPENMUSE_TTS_VOICE", ""),
                                               language_code=lang, sample_rate_hz=TTS_RATE)
        return pcm_to_wav(r.audio, TTS_RATE), "audio/wav"

    # -- OpenAI-compatible ---------------------------------------------------------------
    @staticmethod
    def _post(url: str, key: str, body: bytes, ctype: str) -> bytes:
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": ctype, **({"Authorization": "Bearer " + key} if key else {})})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()

    def _stt_openai(self, wav: bytes) -> str:
        boundary = "om" + uuid.uuid4().hex
        model = os.environ.get("OPENMUSE_STT_MODEL", "whisper-1")
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{model}\r\n'.encode(),
                 f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
                 f'Content-Type: audio/wav\r\n\r\n'.encode() + wav + b"\r\n", f"--{boundary}--\r\n".encode()]
        out = self._post(os.environ["OPENMUSE_STT_BASE"].rstrip("/") + "/audio/transcriptions",
                         os.environ.get("OPENMUSE_STT_KEY", ""), b"".join(parts),
                         f"multipart/form-data; boundary={boundary}")
        return json.loads(out).get("text", "")

    def _tts_openai(self, text: str) -> tuple[bytes, str]:
        body = json.dumps({"model": os.environ.get("OPENMUSE_TTS_MODEL", "tts-1"), "input": text,
                           "voice": os.environ.get("OPENMUSE_TTS_VOICE", "alloy"), "response_format": "mp3"}).encode()
        return self._post(os.environ["OPENMUSE_TTS_BASE"].rstrip("/") + "/audio/speech",
                          os.environ.get("OPENMUSE_TTS_KEY", ""), body, "application/json"), "audio/mpeg"
