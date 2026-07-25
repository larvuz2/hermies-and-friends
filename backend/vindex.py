"""In-memory vector index for brute-force cosine search.

Holds one L2-normalised matrix per *field group* (e.g. "want", "supply",
"need", "offer"), so a single ``query(group, qvec)`` is one matrix-vector dot
product returning cosine similarity to every indexed agent at once. Because
stored and query vectors are unit-normalised, dot product == cosine.

Sized for hundreds-to-a-few-thousand agents on a CPU-only box: at 1000 agents
that is four ``1000 x 384`` float32 matrices (~6 MB) and each query is a ~0.4 M
FLOP dot product — sub-millisecond. Rebuilt from sqlite at startup
(``MatchEngine.build``), then kept in sync on every ``upsert`` / ``remove``.

Not thread-safe by itself; the engine guards mutations with a lock.
"""
import numpy as np


class VectorIndex:
    def __init__(self, dim: int):
        self.dim = dim
        # handle -> {group -> np.ndarray(dim,)}
        self._vecs: "dict[str, dict[str, np.ndarray]]" = {}
        # per-group cached (handles, matrix); invalidated on any write.
        self._cache: "dict[str, tuple[list[str], np.ndarray]]" = {}
        self._dirty = True

    def __len__(self) -> int:
        return len(self._vecs)

    def handles(self) -> "list[str]":
        return list(self._vecs.keys())

    def has(self, handle: str) -> bool:
        return handle in self._vecs

    def upsert(self, handle: str, group_vectors: "dict[str, np.ndarray]") -> None:
        """Store all field-group vectors for one agent."""
        stored = {}
        for group, vec in group_vectors.items():
            stored[group] = np.asarray(vec, dtype=np.float32).reshape(-1)
        self._vecs[handle] = stored
        self._dirty = True
        self._cache.clear()

    def remove(self, handle: str) -> None:
        if self._vecs.pop(handle, None) is not None:
            self._dirty = True
            self._cache.clear()

    def _matrix(self, group: str) -> "tuple[list[str], np.ndarray]":
        """(handles, NxD matrix) for a group, built lazily and cached."""
        cached = self._cache.get(group)
        if cached is not None:
            return cached
        handles: "list[str]" = []
        rows = []
        for handle, groups in self._vecs.items():
            vec = groups.get(group)
            if vec is None:
                vec = np.zeros(self.dim, dtype=np.float32)
            handles.append(handle)
            rows.append(vec)
        matrix = (np.vstack(rows) if rows
                  else np.zeros((0, self.dim), dtype=np.float32))
        result = (handles, matrix)
        self._cache[group] = result
        return result

    def query(self, group: str, qvec: np.ndarray) -> "dict[str, float]":
        """Cosine of ``qvec`` against every agent's vector in ``group``.

        Returns ``{handle: cosine}``. Empty when nothing is indexed.
        """
        handles, matrix = self._matrix(group)
        if not handles:
            return {}
        q = np.asarray(qvec, dtype=np.float32).reshape(-1)
        sims = matrix @ q
        return {h: float(s) for h, s in zip(handles, sims)}
