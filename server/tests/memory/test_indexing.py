"""Tests for transactional IndexingPipeline.index_session (M3.3 / M7).

The pipeline must be all-or-nothing: if the embedder fails mid-batch,
no atoms should be added to the index. The next call retries the same
set unchanged (the embedder is the only step that can fail; the index
itself is idempotent on ``atom_id``).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from openrecall_server.memory.atom import MemoryAtom
from openrecall_server.memory.embeddings import Embedder
from openrecall_server.memory.index import InMemoryMemoryIndex
from openrecall_server.memory.retrieval import IndexingPipeline
from openrecall_server.memory.store import InMemoryAtomStore


def _atom(atom_id: str, session_id: str, text: str) -> MemoryAtom:
    return MemoryAtom(
        atom_id=atom_id,
        session_id=session_id,
        source_event_id=f"e-{atom_id}",
        kind="fact",
        text=text,
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=0,
    )


class FlakyEmbedder:
    """Embedder that raises on the first call, succeeds on later calls.

    Lets us simulate the "embedder fails mid-batch" scenario without
    mocking the protocol.
    """

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("embedder boom")
        return [[1.0, 0.0, 0.0] for _ in texts]


class HappyEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(i), 0.0, 0.0] for i, _ in enumerate(texts)]


def test_indexing_session_atomic_on_embedder_failure():
    """If the embedder fails, no atoms should be in the index."""
    atoms = [_atom(f"a{i}", "s1", f"text {i}") for i in range(3)]
    store = InMemoryAtomStore()
    for a in atoms:
        store.append(a)
    idx = InMemoryMemoryIndex()
    embedder = FlakyEmbedder()
    pipe = IndexingPipeline(store, idx, embedder)
    with pytest.raises(RuntimeError, match="embedder boom"):
        pipe.index_session("s1")
    # No atoms were indexed; the index is empty for this session.
    assert idx.search("s1", [1.0, 0.0, 0.0], 10) == []


def test_indexing_session_retries_after_failure():
    """A successful retry indexes everything that was previously pending."""
    atoms = [_atom(f"a{i}", "s1", f"text {i}") for i in range(3)]
    store = InMemoryAtomStore()
    for a in atoms:
        store.append(a)
    idx = InMemoryMemoryIndex()
    embedder = FlakyEmbedder()
    pipe = IndexingPipeline(store, idx, embedder)
    with pytest.raises(RuntimeError):
        pipe.index_session("s1")
    # Retry — second call succeeds, all atoms now in the index.
    indexed = pipe.index_session("s1")
    assert len(indexed) == 3
    assert len(idx.search("s1", [1.0, 0.0, 0.0], 10)) == 3


def test_indexing_session_skips_already_indexed():
    """Re-running the pipeline is a no-op for already-indexed atoms."""
    atoms = [_atom(f"a{i}", "s1", f"text {i}") for i in range(3)]
    store = InMemoryAtomStore()
    for a in atoms:
        store.append(a)
    idx = InMemoryMemoryIndex()
    pipe = IndexingPipeline(store, idx, HappyEmbedder())
    pipe.index_session("s1")
    # Second call: nothing pending.
    assert pipe.index_session("s1") == []


def test_indexing_session_empty():
    store = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    pipe = IndexingPipeline(store, idx, HappyEmbedder())
    assert pipe.index_session("s1") == []
