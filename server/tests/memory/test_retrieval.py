"""Tests for indexing atoms and answering semantic queries.

IndexingPipeline embeds atoms that aren't indexed yet and adds them to the vector
index (idempotent — already-indexed atoms are skipped, so the embedder isn't
re-called). MemoryRetriever embeds a query and returns the best-matching atoms.
Both use a fake embedder, so no model or network is involved.
"""

from datetime import datetime, timezone

from openrecall_server.memory.atom import MemoryAtom
from openrecall_server.memory.index import InMemoryMemoryIndex
from openrecall_server.memory.retrieval import IndexingPipeline, MemoryRetriever
from openrecall_server.memory.store import InMemoryAtomStore


class FakeEmbedder:
    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.mapping = mapping
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self.mapping[t] for t in texts]


def atom(atom_id: str, text: str, start_ms: int) -> MemoryAtom:
    return MemoryAtom(
        atom_id=atom_id,
        session_id="s1",
        source_event_id="e",
        kind="fact",
        text=text,
        created_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        start_ms=start_ms,
    )


def test_indexing_embeds_pending_atoms_and_is_idempotent():
    embedder = FakeEmbedder({"tea": [1.0, 0.0], "bob": [0.0, 1.0]})
    store = InMemoryAtomStore()
    store.append(atom("a1", "tea", start_ms=0))
    store.append(atom("a2", "bob", start_ms=5000))
    index = InMemoryMemoryIndex()
    pipe = IndexingPipeline(store, index, embedder)

    newly = pipe.index_session("s1")

    assert {a.atom_id for a in newly} == {"a1", "a2"}
    assert index.has("a1") and index.has("a2")

    # re-run indexes nothing and does not re-embed
    assert pipe.index_session("s1") == []
    assert embedder.calls == [["tea", "bob"]]


def test_retriever_embeds_query_and_returns_best_match():
    embedder = FakeEmbedder({"anything about tea?": [1.0, 0.0]})
    index = InMemoryMemoryIndex()
    index.add(atom("a1", "tea", start_ms=0), [1.0, 0.0])
    index.add(atom("a2", "bob", start_ms=5000), [0.0, 1.0])
    retriever = MemoryRetriever(embedder, index)

    results = retriever.query("s1", "anything about tea?", k=1)

    assert [r.atom.atom_id for r in results] == ["a1"]
    assert embedder.calls == [["anything about tea?"]]
