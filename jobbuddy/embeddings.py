"""Embeddings: local sentence-transformers all-MiniLM-L6-v2 (384-dim).

A deterministic hashing embedder is provided for tests and for environments without torch;
it is NOT used in production unless sentence-transformers fails to import.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Protocol, Sequence

import numpy as np

log = logging.getLogger(__name__)
DIM = 384


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class SentenceTransformerEmbedder:
    dim = DIM

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        from sentence_transformers import SentenceTransformer  # heavy import, lazy

        self.name = model_name
        self._model = SentenceTransformer(model_name, device="cpu")  # cached in ~/.cache/huggingface

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vecs = self._model.encode(list(texts), batch_size=32, normalize_embeddings=True,
                                  show_progress_bar=False)
        return [v.astype(float).tolist() for v in vecs]


class HashingEmbedder:
    """Bag-of-words feature hashing into 384 dims, L2-normalized. Deterministic."""

    dim = DIM
    name = "hashing-384"
    _tok = re.compile(r"[a-z0-9][a-z0-9+#.\-]*")

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = np.zeros(self.dim, dtype=float)
            toks = self._tok.findall((t or "").lower())
            grams = toks + [a + " " + b for a, b in zip(toks, toks[1:])]
            for g in grams:
                h = int.from_bytes(hashlib.blake2b(g.encode(), digest_size=8).digest(), "little")
                v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
            n = float(np.linalg.norm(v))
            out.append((v / n).tolist() if n else v.tolist())
        return out


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    va, vb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if not na or not nb or math.isnan(na) or math.isnan(nb):
        return 0.0
    return float(va @ vb / (na * nb))


def build_embedder(model_name: str, allow_fallback: bool = True) -> Embedder:
    try:
        return SentenceTransformerEmbedder(model_name)
    except Exception as exc:  # ImportError, offline, etc.
        if not allow_fallback:
            raise
        log.warning("sentence-transformers unavailable (%s); using hashing embedder", type(exc).__name__)
        return HashingEmbedder()
