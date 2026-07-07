"""Tests for SearchResult carrying the embedding vector (M3.1).

The Retriever needs the candidate vector so it can score without going
back to the embedder (H4 / general efficiency). The vector travels with
the SearchResult so the Scorer has everything it needs in memory.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sense_server.memory.atom import MemoryAtom
from sense_server.memory.index import InMemoryMemoryIndex, SearchResult, SqliteMemoryIndex


def _atom(atom_id: str, session_id: str, text: str = "x") -> MemoryAtom:
    return MemoryAtom(
        atom_id=atom_id,
        session_id=session_id,
        source_event_id=f"e-{atom_id}",
        kind="fact",
        text=text,
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=0,
    )


def test_in_memory_search_result_carries_vector():
    idx = InMemoryMemoryIndex()
    idx.add(_atom("a1", "s1"), [1.0, 0.0, 0.0])
    results = idx.search("s1", [1.0, 0.0, 0.0], k=1)
    assert len(results) == 1
    assert isinstance(results[0], SearchResult)
    assert results[0].vector == [1.0, 0.0, 0.0]
    # Mutating the returned vector must not affect the index (defensive copy).
    results[0].vector.append(99.0)
    assert idx.search("s1", [1.0, 0.0, 0.0], k=1)[0].vector == [1.0, 0.0, 0.0]


def test_sqlite_search_result_carries_vector():
    idx = SqliteMemoryIndex(":memory:")
    idx.add(_atom("a1", "s1"), [1.0, 0.0, 0.0])
    results = idx.search("s1", [1.0, 0.0, 0.0], k=1)
    assert len(results) == 1
    assert results[0].vector == [1.0, 0.0, 0.0]


def test_search_result_default_vector_is_empty_list():
    sr = SearchResult(atom=_atom("a1", "s1"), score=0.5)
    assert sr.vector == []


def test_search_result_score_is_float():
    sr = SearchResult(atom=_atom("a1", "s1"), score=0.5)
    assert isinstance(sr.score, float)
    assert sr.score == 0.5
