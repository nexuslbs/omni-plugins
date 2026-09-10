#!/usr/bin/env python3
"""LOCAL embedding backends for the semantic_search plugin.

Two local backends are supported. Neither one calls an LLM, an embedding
provider or any paid/remote service: the vectors are computed in this process.

  hash (default)
      Deterministic feature-hashing vectorizer built on the Python standard
      library only: word unigrams plus character trigrams are hashed with
      blake2b (stable across processes and machines) into 384 dimensions,
      sublinear term weighting, L2-normalized. Zero bytes to download, about
      1 ms per chunk on CPU, works fully offline.

  fastembed (optional)
      In-process ONNX sentence embeddings (default model
      BAAI/bge-small-en-v1.5, also 384 dims). Fully local inference; requires
      the `fastembed` package (see requirements.txt) and downloads the model
      once (~90 MB). About 10-30 ms per chunk on CPU.

Both backends produce 384-dimensional unit vectors, so the Qdrant collection
dimension is identical whichever backend is configured. Vectors from
different backends are NOT comparable - re-run semantic_search_index after
switching backends.
"""

import hashlib
import math
import re

EMBED_DIM = 384
DEFAULT_HASH_DIM = 384
DEFAULT_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"

_WORD_RE = re.compile(r"[a-z0-9]{2,}")

_CACHE = {}


class EmbedderError(Exception):
    """Raised when the configured local backend cannot be initialized."""


def _features(text):
    """Word unigrams + character trigrams (both case-folded)."""
    low = text.lower()
    for word in _WORD_RE.findall(low):
        yield "w:" + word
    normalized = " " + re.sub(r"\s+", " ", low).strip() + " "
    for i in range(len(normalized) - 2):
        yield "t:" + normalized[i:i + 3]


def hash_embed(text, dim=DEFAULT_HASH_DIM):
    """Deterministic local embedding (feature hashing + L2 normalization)."""
    counts = {}
    for feat in _features(text or ""):
        counts[feat] = counts.get(feat, 0) + 1
    vec = [0.0] * dim
    for feat, count in counts.items():
        digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if (digest[4] & 1) else -1.0
        vec[index] += sign * (1.0 + math.log(count))
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0.0:
        vec = [v / norm for v in vec]
    return vec


class HashEmbedder:
    backend = "hash"

    def __init__(self, dim=DEFAULT_HASH_DIM):
        self.dim = int(dim)

    def embed_documents(self, texts):
        return [hash_embed(t, self.dim) for t in texts]

    def embed_query(self, text):
        return hash_embed(text, self.dim)


class FastEmbedEmbedder:
    backend = "fastembed"

    def __init__(self, model_name=DEFAULT_FASTEMBED_MODEL):
        try:
            from fastembed import TextEmbedding  # type: ignore
        except Exception as exc:  # pragma: no cover - depends on optional dep
            raise EmbedderError(
                "embedding_backend=fastembed requires the optional 'fastembed' "
                "package (pip install fastembed); it is not installed: " + str(exc)
            )
        self.model_name = model_name or DEFAULT_FASTEMBED_MODEL
        self.dim = EMBED_DIM
        self._model = TextEmbedding(model_name=self.model_name)

    def embed_documents(self, texts):
        return [[float(x) for x in vec] for vec in self._model.embed(list(texts))]

    def embed_query(self, text):
        return self.embed_documents([text])[0]


def make_embedder(backend="hash", model_name=DEFAULT_FASTEMBED_MODEL):
    """Return a cached local embedder for the configured backend."""
    backend = (backend or "hash").strip().lower() or "hash"
    key = (backend, model_name or "")
    if key in _CACHE:
        return _CACHE[key]
    if backend == "hash":
        embedder = HashEmbedder()
    elif backend in ("fastembed", "onnx", "local"):
        embedder = FastEmbedEmbedder(model_name)
    else:
        raise EmbedderError(
            "unknown embedding_backend '%s': expected 'hash' or 'fastembed'" % backend
        )
    _CACHE[key] = embedder
    return embedder
