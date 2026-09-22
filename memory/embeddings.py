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


def default_embedder() -> EmbeddingProvider:
    """Deterministic offline embedder unless an API key is configured."""
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAICompatibleEmbedder()
    return DeterministicEmbedder()
