"""Embedders. Both run inside the private network; neither talks to a public service.

HashingEmbedder   deterministic, dependency-free lexical embedder (signed feature hashing of
                  stemmed unigrams and bigrams). Used for tests and offline runs. It is NOT
                  semantic, so paraphrase recall is limited.
OllamaEmbedder    real embeddings from the local model server's /api/embed.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import List, Sequence

import httpx

from .types import RagError

_STOP = frozenset("""a an and are as at be been by can could did do does for from had has have how i if in
into is it its me my of on or our so than that the their them then there these they this to us was we
were what when where which who whom why will with would you your about after all any also more most not
only other some such up out over per each""".split())


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "ly", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _tokens(text: str) -> List[str]:
    return [_stem(t) for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP]


class HashingEmbedder:
    def __init__(self, dim: int = 2048):
        self.dim = dim
        self.id = f"hashing-v1-{dim}"

    def _vec(self, text: str) -> List[float]:
        toks = _tokens(text)
        feats: dict = {}
        for t in toks:
            feats[t] = feats.get(t, 0) + 1.0
        for a, b in zip(toks, toks[1:]):
            k = a + " " + b
            feats[k] = feats.get(k, 0) + 0.5
        v = [0.0] * self.dim
        for k, c in feats.items():
            h = int.from_bytes(hashlib.blake2b(k.encode(), digest_size=8).digest(), "big")
            v[h % self.dim] += (1 if (h >> 63) & 1 else -1) * (1.0 + math.log(c))
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    async def embed(self, texts: Sequence[str]) -> List[List[float]]:
        return [self._vec(t) for t in texts]


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str, timeout: float = 120):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.id = f"ollama-{model}"

    async def embed(self, texts: Sequence[str]) -> List[List[float]]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(self.base_url + "/api/embed", json={"model": self.model, "input": list(texts)})
                r.raise_for_status()
                vecs = r.json()["embeddings"]
        except (httpx.HTTPError, KeyError, ValueError):
            raise RagError("embedder_unavailable") from None
        if len(vecs) != len(texts):
            raise RagError("embedder_bad_response")
        return vecs
