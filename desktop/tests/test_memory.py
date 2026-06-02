"""MemoryStore unit tests: 3-factor retrieval scoring, recency decay, importance
normalization, embedding-matrix rebuild, eviction, and kv round-trip. Plan §12.

Embeddings are passed in as explicit unit vectors so relevance is deterministic
(the store takes raw vectors; the real Embedder is exercised in test_cognition)."""

import time

import numpy as np
import pytest

from memory import MemoryStore, default_importance

T0 = 1_700_000_000.0  # fixed "now" so recency is deterministic


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "memory.sqlite", recency_half_life_s=3600.0)
    yield s
    s.close()


def test_add_and_count(store):
    store.add("dialogue", "hi", embedding=None, source="user")
    store.add("thought", "hmm", source="cognition")
    assert store.count() == 2


def test_relevance_dominates_when_recency_and_importance_tie(store):
    # Same ts (recency ties → 0 after min-max) and same importance (ties → 0),
    # so only relevance differentiates.
    store.add("thought", "apple", importance=0.5, embedding=np.array([1, 0, 0], np.float32), ts=T0)
    store.add("thought", "banana", importance=0.5, embedding=np.array([0, 1, 0], np.float32), ts=T0)
    store.add("thought", "cherry", importance=0.5, embedding=np.array([0, 0, 1], np.float32), ts=T0)
    top = store.retrieve(np.array([1, 0, 0], np.float32), now=T0, k=1)
    assert len(top) == 1 and top[0].content == "apple"


def test_importance_ranks_when_no_query(store):
    store.add("thought", "low", importance=0.1, ts=T0)
    store.add("thought", "mid", importance=0.5, ts=T0)
    store.add("thought", "high", importance=0.9, ts=T0)
    top = store.retrieve(None, now=T0, k=1)  # no embedding/query → relevance all 0
    assert top[0].content == "high"


def test_recency_ranks_when_importance_ties(store):
    store.add("thought", "old", importance=0.5, ts=T0 - 100_000)
    store.add("thought", "fresh", importance=0.5, ts=T0 - 1)
    top = store.retrieve(None, now=T0, k=1)
    assert top[0].content == "fresh"


def test_row_without_embedding_scores_zero_relevance(store):
    # A matching-embedding row beats a no-embedding row when recency/importance tie.
    store.add("thought", "match", importance=0.5, embedding=np.array([1, 0, 0], np.float32), ts=T0)
    store.add("thought", "noemb", importance=0.5, embedding=None, ts=T0)
    top = store.retrieve(np.array([1, 0, 0], np.float32), now=T0, k=1)
    assert top[0].content == "match"


def test_matrix_rebuilds_after_add(store):
    store.add("thought", "a", embedding=np.array([1, 0, 0], np.float32), ts=T0)
    assert store.retrieve(np.array([1, 0, 0], np.float32), now=T0, k=1)[0].content == "a"
    # Add a second; the cached matrix must rebuild so the new row is retrievable.
    store.add("thought", "b", embedding=np.array([0, 1, 0], np.float32), ts=T0)
    assert store.retrieve(np.array([0, 1, 0], np.float32), now=T0, k=1)[0].content == "b"


def test_accumulated_importance_since(store):
    store.add("thought", "before", importance=0.3, ts=T0 - 10)
    store.add("thought", "after1", importance=0.4, ts=T0 + 10)
    store.add("thought", "after2", importance=0.6, ts=T0 + 20)
    assert store.accumulated_importance_since(T0) == pytest.approx(1.0)


def test_evict_drops_least_important_oldest(store):
    for i, imp in enumerate([0.1, 0.2, 0.3, 0.4, 0.5]):
        store.add("thought", f"m{i}", importance=imp, ts=T0 + i)
    removed = store.evict(3)
    assert removed == 2 and store.count() == 3
    kept = {m.content for m in store.recent(10)}
    assert kept == {"m2", "m3", "m4"}  # the two lowest-importance dropped


def test_kv_round_trip(store):
    assert store.kv_get("mood") is None
    store.kv_set("mood", '{"p":0.1}')
    assert store.kv_get("mood") == '{"p":0.1}'
    store.kv_set("mood", '{"p":0.5}')  # upsert
    assert store.kv_get("mood") == '{"p":0.5}'


def test_persists_across_reopen(tmp_path):
    path = tmp_path / "m.sqlite"
    s1 = MemoryStore(path)
    s1.add("reflection", "I've noticed the human likes mornings.", source="reflect")
    s1.kv_set("last_reflect_ts", str(T0))
    s1.close()
    s2 = MemoryStore(path)
    assert s2.count() == 1
    assert s2.kv_get("last_reflect_ts") == str(T0)
    assert s2.recent(1)[0].kind == "reflection"
    s2.close()


def test_default_importance_by_kind():
    assert default_importance("reflection") > default_importance("dialogue")
    assert default_importance("dialogue") > default_importance("thought")
    assert default_importance("unknown") == 0.5


def test_retrieve_uses_wall_clock_when_now_omitted(store):
    store.add("thought", "x", embedding=np.array([1, 0, 0], np.float32), ts=time.time())
    assert store.retrieve(np.array([1, 0, 0], np.float32), k=1)  # no `now` → time.time()
