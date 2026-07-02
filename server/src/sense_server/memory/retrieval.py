"""Indexing and retrieval over §G memory atoms.

* :class:`IndexingPipeline` — embeds atoms that aren't indexed yet and adds them to
  the vector index. Idempotent: already-indexed atoms are skipped, so re-running is
  cheap and never re-embeds. A natural out-of-band companion to extraction.
* :class:`MemoryRetriever` — embeds a query with the same (pluggable) embedder and
  returns the best-matching atoms for a session.

The embedder is injected and provider-agnostic, so the embedding model is config,
not code.
"""

from __future__ import annotations

from .atom import MemoryAtom
from .embeddings import Embedder
from .index import MemoryIndex, SearchResult
from .store import AtomStore


class IndexingPipeline:
    def __init__(self, atom_store: AtomStore, index: MemoryIndex, embedder: Embedder) -> None:
        self._atoms = atom_store
        self._index = index
        self._embedder = embedder

    def index_session(self, session_id: str) -> list[MemoryAtom]:
        """Embed and index any not-yet-indexed atoms; return the newly indexed ones."""
        pending = [a for a in self._atoms.atoms(session_id) if not self._index.has(a.atom_id)]
        if not pending:
            return []
        vectors = self._embedder.embed([a.text for a in pending])
        for atom, vector in zip(pending, vectors):
            self._index.add(atom, vector)
        return pending


class MemoryRetriever:
    def __init__(self, embedder: Embedder, index: MemoryIndex) -> None:
        self._embedder = embedder
        self._index = index

    def query(self, session_id: str, text: str, k: int = 5) -> list[SearchResult]:
        """Return the top-k atoms in the session most similar to ``text``."""
        query_vector = self._embedder.embed([text])[0]
        return self._index.search(session_id, query_vector, k)
