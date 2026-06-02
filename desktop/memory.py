"""Generative-Agents-style memory stream. Plan §12 (cognition/memory).

A persistent, ever-growing stream of the robot's experiences — ``dialogue`` it
heard/said, ``resolution`` facts a human taught it, and its own ``thought`` /
``reflection`` notes — each with an *importance* score and an optional 384-d
embedding. Retrieval scores candidates by **recency × importance × relevance**
(Park et al. 2023): each component min-max normalized over the candidate set,
then summed with configurable weights. Survives restarts; injected into the
agent's prompt each turn so behavior changes as the stream grows.

Per the locked memory-scope decision, raw camera observations are **not** stored
here (vision is used only in the moment); a ``thought`` may *mention* what was
seen, but no ``observation`` kind is written.

Storage mirrors ``pet_queue.QueueDB``: a single ``sqlite3`` connection guarded by
a ``threading.Lock`` (the async orchestrator calls these via ``run_in_executor``).
Embeddings live as ``BLOB``s; retrieval decodes them into a cached numpy matrix
(rebuilt only after a write), so relevance is a single ``mat @ q`` — sub-ms at the
few-thousand-row scale this reaches. No native vector extension (Windows-clean).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          REAL NOT NULL,          -- epoch seconds (creation)
  kind        TEXT NOT NULL,          -- dialogue | resolution | thought | reflection
  content     TEXT NOT NULL,
  importance  REAL NOT NULL,          -- 0..1
  embedding   BLOB,                   -- float32 np.tobytes(), may be NULL
  last_access REAL NOT NULL,          -- reserved (set = ts; for future access-based eviction)
  source      TEXT                    -- user | resolve | cognition | reflect | ...
);
CREATE INDEX IF NOT EXISTS idx_mem_ts ON memories(ts);
CREATE INDEX IF NOT EXISTS idx_mem_importance ON memories(importance);

CREATE TABLE IF NOT EXISTS kv (
  k  TEXT PRIMARY KEY,
  v  TEXT NOT NULL
);
"""

# Default heuristic importance by kind (0..1), used when a caller doesn't pass one.
# A cheap stand-in for the Generative-Agents LLM importance rating; keeps the
# frequently-ticking small model from paying an extra call per memory write.
IMPORTANCE: dict[str, float] = {
    "resolution": 0.8,   # a human taught it something — it mattered enough to defer
    "dialogue": 0.6,     # things heard/said
    "thought": 0.25,     # routine idle monologue
    "reflection": 0.9,   # synthesized insight — should float to the top of recall
}

DEFAULT_RECENCY_HALF_LIFE_S = 21600.0  # 6 h


def default_importance(kind: str) -> float:
    return IMPORTANCE.get(kind, 0.5)


def _minmax(a: np.ndarray) -> np.ndarray:
    """Min-max normalize to [0,1]; all-equal (or empty) → zeros (no differentiation)."""
    if a.size == 0:
        return a
    lo = float(a.min())
    hi = float(a.max())
    if hi - lo < 1e-9:
        return np.zeros_like(a)
    return (a - lo) / (hi - lo)


@dataclass
class Memory:
    id: int
    ts: float
    kind: str
    content: str
    importance: float
    source: Optional[str] = None


class MemoryStore:
    def __init__(self, db_path: str | Path, recency_half_life_s: float = DEFAULT_RECENCY_HALF_LIFE_S):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.recency_half_life_s = recency_half_life_s
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        # Cached embedding matrix + the rows it was built from (rebuilt when _dirty).
        self._dirty = True
        self._rows_cache: list[sqlite3.Row] = []
        self._mat: Optional[np.ndarray] = None          # (M, dim) for rows that have an embedding
        self._mat_pos: np.ndarray = np.empty(0, np.int64)  # index of each matrix row into _rows_cache

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- write ----------------------------------------------------------------
    def add(
        self,
        kind: str,
        content: str,
        importance: Optional[float] = None,
        embedding: Optional[np.ndarray] = None,
        source: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> int:
        """Append one memory; returns its id. ``importance`` defaults by ``kind``."""
        ts = time.time() if ts is None else ts
        imp = default_importance(kind) if importance is None else float(importance)
        blob = embedding.astype(np.float32).tobytes() if embedding is not None else None
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO memories (ts, kind, content, importance, embedding, last_access, source)"
                " VALUES (?,?,?,?,?,?,?)",
                (ts, kind, content, imp, blob, ts, source),
            )
            self._conn.commit()
            self._dirty = True
            return int(cur.lastrowid)

    # --- retrieval (recency × importance × relevance) -------------------------
    def retrieve(
        self,
        query_embedding: Optional[np.ndarray],
        now: Optional[float] = None,
        k: int = 5,
        w_recency: float = 1.0,
        w_importance: float = 1.0,
        w_relevance: float = 1.0,
    ) -> list[Memory]:
        """Top-``k`` memories by the weighted, min-max-normalized 3-factor score.

        Rows without an embedding (or when ``query_embedding`` is None) get a
        relevance of 0 — they can still surface on recency/importance alone.
        """
        now = time.time() if now is None else now
        with self._lock:
            self._refresh_cache_locked()
            rows = self._rows_cache
            if not rows:
                return []
            recency = np.array(
                [0.5 ** ((now - r["ts"]) / self.recency_half_life_s) for r in rows],
                dtype=np.float32,
            )
            importance = np.array([r["importance"] for r in rows], dtype=np.float32)
            relevance = np.zeros(len(rows), dtype=np.float32)
            if self._mat is not None and query_embedding is not None:
                q = np.asarray(query_embedding, dtype=np.float32)
                relevance[self._mat_pos] = self._mat @ q
            score = (
                w_recency * _minmax(recency)
                + w_importance * _minmax(importance)
                + w_relevance * _minmax(relevance)
            )
            order = np.argsort(-score)[:k]
            return [self._to_mem(rows[i]) for i in order]

    def recent(self, k: int = 10) -> list[Memory]:
        """The ``k`` most-recent memories, newest first (a recency-only fallback)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memories ORDER BY ts DESC, id DESC LIMIT ?", (k,)
            ).fetchall()
        return [self._to_mem(r) for r in rows]

    def accumulated_importance_since(self, ts: float) -> float:
        """Sum of importance for memories created after ``ts`` (drives reflection)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(importance), 0) AS s FROM memories WHERE ts > ?", (ts,)
            ).fetchone()
        return float(row["s"])

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def evict(self, max_rows: int) -> int:
        """Trim to ``max_rows`` by deleting the least-important, oldest rows. 0 = no-op."""
        if max_rows <= 0:
            return 0
        with self._lock:
            n = int(self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            if n <= max_rows:
                return 0
            to_del = n - max_rows
            self._conn.execute(
                "DELETE FROM memories WHERE id IN ("
                "  SELECT id FROM memories ORDER BY importance ASC, ts ASC LIMIT ?)",
                (to_del,),
            )
            self._conn.commit()
            self._dirty = True
            return to_del

    # --- kv (mood + bookkeeping) ----------------------------------------------
    def kv_get(self, k: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row["v"] if row else None

    def kv_set(self, k: str, v: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, v),
            )
            self._conn.commit()

    # --- internals ------------------------------------------------------------
    def _refresh_cache_locked(self) -> None:
        """Rebuild the rows snapshot + embedding matrix if a write happened. Lock held."""
        if not self._dirty and self._rows_cache:
            return
        rows = self._conn.execute(
            "SELECT id, ts, kind, content, importance, embedding, last_access, source"
            " FROM memories ORDER BY id"
        ).fetchall()
        self._rows_cache = rows
        vecs: list[np.ndarray] = []
        pos: list[int] = []
        for i, r in enumerate(rows):
            if r["embedding"] is not None:
                vecs.append(np.frombuffer(r["embedding"], dtype=np.float32))
                pos.append(i)
        self._mat = np.vstack(vecs) if vecs else None
        self._mat_pos = np.array(pos, dtype=np.int64)
        self._dirty = False

    @staticmethod
    def _to_mem(r: sqlite3.Row) -> Memory:
        return Memory(
            id=int(r["id"]), ts=float(r["ts"]), kind=r["kind"], content=r["content"],
            importance=float(r["importance"]), source=r["source"],
        )
