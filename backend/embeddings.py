"""Text embedding layer for the semantic matching engine.

Two interchangeable encoders behind one `Encoder` protocol:

  * ``FastEmbedEncoder`` — real semantic vectors from fastembed's ONNX build of
    ``BAAI/bge-small-en-v1.5`` (384-dim). ~100 MB model, downloaded and cached
    on first use (needs outbound internet once). CPU-only, no torch.
  * ``HashEmbedder`` — a deterministic, dependency-light (numpy-only) fallback.
    Hashes word tokens + character n-grams into a fixed 384-dim space so cosine
    similarity stays meaningful for literal / fuzzy token overlap. This keeps
    the hub matching (never down) when fastembed cannot import or the model
    cannot download, and lets the whole test suite run without the model.

Selection (``get_encoder``):
  * ``HERMIX_FORCE_FALLBACK_EMBED=1`` (or ``true``/``yes``) forces the fallback
    encoder — used by the test suite so it never touches the network.
  * otherwise try fastembed; on ANY import/load failure log a clear warning and
    fall back. The active mode is always inspectable via ``encoder.mode``.

All encoders return an ``np.ndarray`` of shape ``(n, dim)``, float32, with each
row L2-normalised so cosine similarity is a plain dot product.
"""
import hashlib
import logging
import os
import re
import threading
from typing import Protocol, runtime_checkable

import numpy as np

try:
    import compat_env
except ImportError:  # loaded by path from outside backend/ (evals, tooling)
    import pathlib as _pl
    import sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import compat_env

log = logging.getLogger("hermix.embeddings")

DIM = 384
MODEL_NAME = "BAAI/bge-small-en-v1.5"

_SPLIT = re.compile(r"[^a-z0-9]+")


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


@runtime_checkable
class Encoder(Protocol):
    """A text -> matrix encoder. Rows are L2-normalised float32 vectors."""

    dim: int
    mode: str        # "fastembed" | "fallback"
    model_name: str
    sim_floor: float  # expected cosine of unrelated text, for calibration

    def encode(self, texts: "list[str]") -> np.ndarray:
        ...


def _l2_normalise(mat: np.ndarray) -> np.ndarray:
    mat = np.asarray(mat, dtype=np.float32)
    if mat.ndim == 1:
        mat = mat.reshape(1, -1)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype(np.float32)


class HashEmbedder:
    """Deterministic hashing n-gram pseudo-embedding (numpy only, no network).

    For each input string we accumulate signed hashed features from:
      * whole normalised phrase,
      * word tokens (higher weight),
      * character trigrams of each token (fuzzy overlap: "visuals" ~ "visualizers").
    Hashing uses md5 (stable across processes, unlike Python's salted ``hash``),
    so vectors are reproducible for the persisted-index round-trip.
    """

    mode = "fallback"
    model_name = "hash-ngram-v1"
    sim_floor = 0.0        # unrelated text -> ~0 cosine, no calibration needed

    def __init__(self, dim: int = DIM):
        self.dim = dim

    @staticmethod
    def _hash(token: str) -> int:
        return int.from_bytes(hashlib.md5(token.encode("utf-8")).digest()[:8], "big")

    def _feature(self, vec: np.ndarray, token: str, weight: float) -> None:
        h = self._hash(token)
        idx = h % self.dim
        sign = 1.0 if (h >> 63) & 1 else -1.0
        vec[idx] += sign * weight

    def _embed_one(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        norm = str(text or "").lower().strip()
        if not norm:
            return vec
        self._feature(vec, "\x00" + norm, 1.5)          # whole-phrase feature
        for tok in _SPLIT.split(norm):
            if not tok:
                continue
            self._feature(vec, tok, 2.0)                 # word token
            padded = f"#{tok}#"
            for i in range(len(padded) - 2):             # char trigrams (fuzzy)
                self._feature(vec, "3:" + padded[i:i + 3], 0.6)
        return vec

    def encode(self, texts: "list[str]") -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        mat = np.vstack([self._embed_one(t) for t in texts])
        return _l2_normalise(mat)


class FastEmbedEncoder:
    """Real 384-dim semantic vectors via fastembed (ONNX, CPU)."""

    mode = "fastembed"
    model_name = MODEL_NAME
    # bge-small cosine of unrelated English text sits well above zero; calibrate
    # so the engine's components still span a useful 0..1 range.
    sim_floor = 0.35

    def __init__(self):
        from fastembed import TextEmbedding  # imported lazily; may raise

        self._model = TextEmbedding(model_name=MODEL_NAME)
        # Probe once so a broken model/download fails here (caught by factory),
        # not on the first live request.
        probe = np.array(list(self._model.embed(["hermix"])), dtype=np.float32)
        self.dim = int(probe.shape[1])

    def encode(self, texts: "list[str]") -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        # fastembed skips falsy strings oddly; feed a space for empty inputs and
        # zero them out afterwards so shape stays aligned with `texts`.
        safe = [t if (t and t.strip()) else " " for t in texts]
        mat = np.array(list(self._model.embed(safe)), dtype=np.float32)
        mat = _l2_normalise(mat)
        for i, t in enumerate(texts):
            if not (t and t.strip()):
                mat[i] = 0.0
        return mat


# --- factory --------------------------------------------------------------
_lock = threading.Lock()
_cache: "dict[str, Encoder]" = {}


def _build_encoder() -> Encoder:
    if _truthy(compat_env.env("HERMIX_FORCE_FALLBACK_EMBED")):
        log.warning("HERMIX_FORCE_FALLBACK_EMBED set — using hashing fallback embeddings")
        return HashEmbedder()
    try:
        enc = FastEmbedEncoder()
        log.info("embeddings: fastembed active (model=%s, dim=%d)", enc.model_name, enc.dim)
        return enc
    except Exception as exc:  # ImportError, download/runtime failure, etc.
        log.warning(
            "embeddings: fastembed unavailable (%s) — falling back to hashing "
            "pseudo-embeddings; hub stays up but matching is token-based",
            exc,
        )
        return HashEmbedder()


def get_encoder() -> Encoder:
    """Return the process-wide encoder singleton (built once, thread-safe).

    Keyed on the forced-fallback flag so tests and prod can each cache their own
    mode without reloading the ~100 MB model per request.
    """
    key = "fallback" if _truthy(compat_env.env("HERMIX_FORCE_FALLBACK_EMBED")) else "auto"
    enc = _cache.get(key)
    if enc is None:
        with _lock:
            enc = _cache.get(key)
            if enc is None:
                enc = _build_encoder()
                _cache[key] = enc
    return enc


def reset_encoder_cache() -> None:
    """Drop the cached encoder(s). For tests that flip embedding modes."""
    with _lock:
        _cache.clear()
