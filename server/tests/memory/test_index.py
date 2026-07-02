"""Tests for the memory vector index (semantic search over §G atoms).

The index stores each atom alongside its embedding and answers top-k cosine-similarity
queries, scoped per session. Brute-force cosine is exact and dependency-free; a
pgvector backend is a later scale optimisation that must satisfy this same suite.
Vectors here are tiny and hand-chosen so rankings are deterministic.
"""

from datetime import datetime, timezone

import pytest

from sense_server.memory.atom import MemoryAtom
from sense_server.memory.index import InMemoryMemoryIndex, SqliteMemoryIndex


def atom(atom_id: str, session_id: str = "s1", text: str = "t", start_ms: int = 0) -> MemoryAtom:
    return MemoryAtom(
        atom_id=atom_id,
        session_id=session_id,
        source_event_id="e",
        kind="fact",
        text=text,
        created_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        start_ms=start_ms,
    )


@pytest.fixture(params=["memory", "sqlite"])
def index(request, tmp_path):
    if request.param == "memory":
        return InMemoryMemoryIndex()
    return SqliteMemoryIndex(tmp_path / "index.db")


def test_add_then_search_returns_the_atom(index):
    assert index.add(atom("a1", text="tea"), [1.0, 0.0]) is True

    results = index.search("s1", [1.0, 0.0], k=5)
    assert [r.atom.atom_id for r in results] == ["a1"]
    assert results[0].score == pytest.approx(1.0)


def test_add_is_idempotent_by_atom_id(index):
    assert index.add(atom("a1"), [1.0, 0.0]) is True
    assert index.add(atom("a1"), [1.0, 0.0]) is False
    assert index.has("a1") is True
    assert index.has("nope") is False


def test_search_ranks_by_cosine_similarity_and_respects_k(index):
    index.add(atom("east", text="east"), [1.0, 0.0])
    index.add(atom("north", text="north"), [0.0, 1.0])
    index.add(atom("ne", text="north-east"), [1.0, 1.0])

    results = index.search("s1", [1.0, 0.0], k=2)

    assert [r.atom.atom_id for r in results] == ["east", "ne"]  # north (orthogonal) excluded
    assert results[0].score == pytest.approx(1.0)
    assert results[1].score == pytest.approx(0.7071, abs=1e-3)


def test_search_is_scoped_per_session(index):
    index.add(atom("a1", session_id="s1", text="x"), [1.0, 0.0])
    index.add(atom("b1", session_id="s2", text="x"), [1.0, 0.0])

    results = index.search("s1", [1.0, 0.0], k=5)
    assert [r.atom.atom_id for r in results] == ["a1"]


def test_zero_query_vector_does_not_blow_up(index):
    index.add(atom("a1"), [1.0, 0.0])
    results = index.search("s1", [0.0, 0.0], k=5)
    assert all(r.score == 0.0 for r in results)


def test_sqlite_index_persists_across_reopen(tmp_path):
    path = tmp_path / "index.db"
    SqliteMemoryIndex(path).add(atom("a1", text="durable"), [0.5, 0.5])

    reopened = SqliteMemoryIndex(path)
    results = reopened.search("s1", [0.5, 0.5], k=1)
    assert results[0].atom.text == "durable"
