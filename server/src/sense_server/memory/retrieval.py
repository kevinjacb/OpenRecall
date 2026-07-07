"""Indexing and retrieval over §G memory atoms.

* :class:`IndexingPipeline` — embeds atoms that aren't indexed yet and adds them to
  the vector index. Idempotent: already-indexed atoms are skipped, so re-running is
  cheap and never re-embeds. A natural out-of-band companion to extraction.
* :class:`Retriever` — the new read-path seam. Embeds a query with the same
  (pluggable) embedder, asks the index for candidates, and ranks them with
  the injected :class:`~sense_server.memory.scoring.Scorer`. Global by
  default; an optional ``session_id`` narrows the search. Read-only
  (INV-6) — never mutates the index, the atom store, or the cursor.
* :class:`MemoryRetriever` — DEPRECATED. Kept for backward compatibility
  with the existing unit tests; will be removed in a follow-up.

The embedder, scorer, clock, and id generator are all injected and
provider-agnostic, so embedding model, scoring strategy, clock source,
and id format are all config, not code.
"""
from __future__ import annotations

import time
import warnings
from datetime import datetime

from ..contracts.clock import Clock
from ..contracts.id_generator import IdGenerator
from ..contracts.types import (
    Provenance,
    RetrieverContext,
    RetrievedContext,
    ScoredAtom,
)
from .atom import MemoryAtom
from .embeddings import Embedder
from .index import MemoryIndex, SearchResult
from .scoring import Scorer
from .store import AtomStore


class IndexingPipeline:
    def __init__(self, atom_store: AtomStore, index: MemoryIndex, embedder: Embedder) -> None:
        self._atoms = atom_store
        self._index = index
        self._embedder = embedder

    def index_session(self, session_id: str) -> list[MemoryAtom]:
        """Embed and index any not-yet-indexed atoms; return the newly indexed ones.

        The pipeline is **transactional**: the embedder is called once for
        the whole batch. If the embedder raises, nothing is added to the
        index — the next call retries the same set (M3.3 / M7).
        """
        pending = [a for a in self._atoms.atoms(session_id) if not self._index.has(a.atom_id)]
        if not pending:
            return []
        # May raise — caller should let it propagate so the worker can
        # record an INDEXING_FAILURES_TOTAL and skip the cursor advance.
        vectors = self._embedder.embed([a.text for a in pending])
        for atom, vector in zip(pending, vectors):
            self._index.add(atom, vector)
        return pending


class Retriever:
    """The read-path seam: query -> :class:`RetrievedContext`.

    Pure function over its dependencies. Read-only (INV-6) — every
    call is idempotent with no observable side effects on the index,
    the atom store, or the extraction cursor. Deterministic (INV-7) —
    same inputs produce the same atom order, every time.
    """

    def __init__(
        self,
        embedder: Embedder,
        index: MemoryIndex,
        scorer: Scorer,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._embedder = embedder
        self._index = index
        self._scorer = scorer
        self._clock = clock
        self._ids = ids

    def retrieve(self, ctx: RetrieverContext) -> RetrievedContext:
        start = time.monotonic()
        # 1. trace id for this retrieval — captured up front so every
        #    downstream consumer can cite the same id.
        trace_id = self._ids.new()
        # 2. embed the query once.
        query_vec = self._embedder.embed([ctx.query_text])[0]
        # 3. fetch candidates. The index's session_id filter is empty
        #    string for global retrieval — that path is opt-in by the
        #    caller via session_id; here we always pass a literal.
        candidates = self._index.search(ctx.session_id, query_vec, ctx.limit * 4)
        # 4. score. now() comes from the injected clock.
        now = self._clock.now()
        scored: list[ScoredAtom] = []
        for sr in candidates:
            age_s = max(0.0, (now - sr.atom.created_at).total_seconds())
            score = self._scorer.score(query_vec, sr.vector, age_s)
            if score <= 0.0:
                continue
            scored.append(
                ScoredAtom(
                    atom_id=sr.atom.atom_id,
                    session_id=sr.atom.session_id,
                    kind=sr.atom.kind,
                    text=sr.atom.text,
                    created_at=sr.atom.created_at,
                    start_ms=sr.atom.start_ms,
                    score=score,
                    source_event_id=sr.atom.source_event_id,
                    provenance=sr.atom.to_provenance() if hasattr(sr.atom, "to_provenance") else None,
                )
            )
        # 5. deterministic ordering: score desc, created_at desc, atom_id asc (INV-7).
        scored.sort(
            key=lambda a: (
                -a.score,
                -a.created_at.timestamp(),
                a.atom_id,
            )
        )
        scored = scored[: ctx.limit]
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return RetrievedContext(
            atoms=tuple(scored),
            retrieval_strategy=self._scorer.name,
            scorer_version=self._scorer.version,
            index_name=self._index.name,
            index_version=self._index.version,
            top_score=scored[0].score if scored else float("-inf"),
            lowest_score=scored[-1].score if scored else float("-inf"),
            returned_count=len(scored),
            retrieval_latency_ms=elapsed_ms,
            candidate_count=len(candidates),
            session_filter=ctx.session_id,
            retrieval_trace_id=trace_id,
        )


class MemoryRetriever:
    """DEPRECATED — kept for backward compatibility with existing tests.

    Use :class:`Retriever` for new code. The new class is read-only,
    produces a :class:`RetrievedContext` with full audit metadata, and
    uses the injected :class:`Scorer` (so the scoring strategy is
    config, not code).
    """

    def __init__(self, embedder: Embedder, index: MemoryIndex) -> None:
        warnings.warn(
            "MemoryRetriever is deprecated; use Retriever instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._embedder = embedder
        self._index = index

    def query(self, session_id: str, text: str, k: int = 5) -> list[SearchResult]:
        """Return the top-k atoms in the session most similar to ``text``."""
        query_vector = self._embedder.embed([text])[0]
        return self._index.search(session_id, query_vector, k)
