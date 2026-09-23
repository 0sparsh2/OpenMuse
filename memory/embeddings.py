"""
Embedding providers for semantic memory recall.

The production path is a real embedding model (the blueprint names it
"embed-v3") behind this interface, with vectors living in pgvector. Phase 2
ships a deterministic offline embedder so the whole memory system — and its
tests — runs without network access or an API key.

DeterministicEmbedder design (pure Python, no extra dependencies):
  - The text is tokenized into word tokens plus character 3-grams (the
    n-grams give genuine fuzzy/paraphrase matching instead of exact-token
    overlap only).
  - Each token is hashed into a fixed-dimension vector via SHA-256 seeded
    per-dimension draws in [-1, 1]; token vectors are summed and L2
    normalized. This is a random-projection bag-of-tokens embedding:
    deterministic, stable across processes, and good enough for recall
    ranking when combined with the lexical signal in the hybrid score.

Swap in OpenAICompatibleEmbedder (env-configured) for production quality.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from abc import ABC, abstractmethod

DIMENSION = 256
_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    words = _WORD_RE.findall(text.lower())
    toks: list[str] = []
    for w in words:
        toks.append("w:" + w)
        if len(w) >= 3:
            for i in range(len(w) - 2):
                toks.append("g:" + w[i:i + 3])
    return toks


def _token_vector(token: str, dim: int) -> list[float]:
    vec = [0.0] * dim
    for i in range(dim):
        digest = hashlib.sha256(f"{token}:{i}".encode("utf-8")).digest()
        u = int.from_bytes(digest[:8], "big") / 2**64  # [0, 1)
        vec[i] = u * 2.0 - 1.0
    return vec


def cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(y * y for y in b))
    if da == 0.0 or db == 0.0:
        return 0.0
    return max(-1.0, min(1.0, num / (da * db)))


class EmbeddingProvider(ABC):
    name: str = "embed-base"
    dimension: int = DIMENSION

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one L2-normalized vector per input text."""

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query. Asymmetric retrieval models (e.g. NIM
        embedqa/nemotron-embed) encode queries differently from passages."""
        return self.embed_one(text)


class DeterministicEmbedder(EmbeddingProvider):
    """Offline, deterministic embedder. No network, no API key.

    Stable across processes and machines: identical text always yields the
    identical vector. Intended for development, tests, and air-gapped
    deployments — not as a substitute for a trained embedding model.
    """
    name = "embed-deterministic@1.0.0"

    def __init__(self, dimension: int = DIMENSION):
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            acc = [0.0] * self.dimension
            for tok in _tokens(text or ""):
                tv = _token_vector(tok, self.dimension)
                for i in range(self.dimension):
                    acc[i] += tv[i]
            norm = math.sqrt(sum(x * x for x in acc))
            out.append([x / norm for x in acc] if norm else acc)
        return out


class OpenAICompatibleEmbedder(EmbeddingProvider):
    """Production-quality embedder via an OpenAI-compatible embeddings API.

    Configured exactly like the gateway's OpenAI adapter:
      OPENAI_API_KEY (required), OPENAI_BASE_URL (default api.openai.com),
      OPENAI_EMBED_MODEL (default text-embedding-3-small).
    Vectors are L2-normalized on the way in so cosine == dot product.
    """
    name = "embed-openai-compat@1.0.0"

    def __init__(self, model: str = "", api_key: str = "", base_url: str = ""):
        import requests  # local import: only needed when this provider is used
        self._requests = requests
        self.model = model or os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL",
                                                    "https://api.openai.com")).rstrip("/")
        if not self.api_key:
            raise RuntimeError("OpenAICompatibleEmbedder needs OPENAI_API_KEY (or api_key=)")
        self.dimension = 0  # discovered from the first response

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._requests.post(
            f"{self.base_url}/v1/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        data.sort(key=lambda d: d["index"])
        out = []
        for d in data:
            vec = [float(x) for x in d["embedding"]]
            if not self.dimension:
                self.dimension = len(vec)
            norm = math.sqrt(sum(x * x for x in vec))
            out.append([x / norm for x in vec] if norm else vec)
        return out


class NimEmbedder(EmbeddingProvider):
    """NVIDIA NIM embeddings (OpenAI-compatible /embeddings + input_type).

    Stored text is embedded as "passage", searches as "query". Batches of 32,
    retried on 429/5xx, over-long inputs truncated server-side (truncate=END).

    Configured via NVIDIA_NIM_API_KEY, NVIDIA_NIM_API_BASE and
    NVIDIA_EMBED_MODEL (default nvidia/nemotron-3-embed-1b).
    """

    BATCH = 32

    def __init__(self, model: str = "", api_key: str = "", base_url: str = ""):
        import requests
        self._requests = requests
        self.model = model or os.environ.get("NVIDIA_EMBED_MODEL", "nvidia/nemotron-3-embed-1b")
        self.api_key = api_key or os.environ.get("NVIDIA_NIM_API_KEY", "")
        self.base_url = (base_url or os.environ.get(
            "NVIDIA_NIM_API_BASE", "https://integrate.api.nvidia.com/v1")).rstrip("/")
        if not self.api_key:
            raise RuntimeError("NimEmbedder needs NVIDIA_NIM_API_KEY (or api_key=)")
        self.name = f"embed-nim:{self.model}"
        self.dimension = 0

    def _post(self, texts: list[str], input_type: str) -> list[list[float]]:
        import time as _time
        out: list[list[float]] = []
        for i in range(0, len(texts), self.BATCH):
            batch = [t if t.strip() else "(empty)" for t in texts[i:i + self.BATCH]]
            for attempt in range(4):
                resp = self._requests.post(
                    f"{self.base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": self.model, "input": batch, "input_type": input_type,
                          "encoding_format": "float", "truncate": "END"},
                    timeout=60)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                    _time.sleep(1.5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                break
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            for d in data:
                vec = [float(x) for x in d["embedding"]]
                self.dimension = self.dimension or len(vec)
                norm = math.sqrt(sum(x * x for x in vec))
                out.append([x / norm for x in vec] if norm else vec)
        return out

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._post(texts, "passage")

    def embed_query(self, text: str) -> list[float]:
        return self._post([text], "query")[0]


class CachedEmbedder(EmbeddingProvider):
    """In-process cache in front of a remote embedder (recall re-embeds the
    same claims on every search; this keeps that to one call per text)."""

    def __init__(self, inner: EmbeddingProvider, max_items: int = 50_000):
        self.inner = inner
        self.name = inner.name
        self.max_items = max_items
        self._cache: dict[tuple[str, str], list[float]] = {}

    @property
    def dimension(self) -> int:  # type: ignore[override]
        return self.inner.dimension

    def _remember(self, key, vec) -> None:
        if len(self._cache) >= self.max_items:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = vec

    def embed(self, texts: list[str]) -> list[list[float]]:
        missing = [t for t in dict.fromkeys(texts) if ("p", t) not in self._cache]
        if missing:
            for t, v in zip(missing, self.inner.embed(missing)):
                self._remember(("p", t), v)
        return [self._cache[("p", t)] for t in texts]

    def embed_query(self, text: str) -> list[float]:
        key = ("q", text)
        if key not in self._cache:
            self._remember(key, self.inner.embed_query(text))
        return self._cache[key]


_DEFAULT: EmbeddingProvider | None = None


def default_embedder() -> EmbeddingProvider:
    """NIM embeddings when an NVIDIA key is configured, then OpenAI-compatible,
    else the deterministic offline embedder. Shared per process (one cache)."""
    global _DEFAULT
    if _DEFAULT is not None:
        return _DEFAULT
    if os.environ.get("NVIDIA_NIM_API_KEY") and os.environ.get("OPENMUSE_EMBEDDINGS", "nim") == "nim":
        _DEFAULT = CachedEmbedder(NimEmbedder())
    elif os.environ.get("OPENAI_API_KEY"):
        _DEFAULT = CachedEmbedder(OpenAICompatibleEmbedder())
    else:
        _DEFAULT = DeterministicEmbedder()
    return _DEFAULT
