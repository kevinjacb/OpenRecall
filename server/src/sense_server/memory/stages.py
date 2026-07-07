"""Four explicit pipeline stages (M4.2).

The :class:`ExtractionPipeline` is decomposed into four named, individually
testable stages. Each stage is a pure function over the prior stage's
output (plus its injected dependencies). The :class:`Pipeline` class
composes them so the production code path looks like:

    events → ExtractionStage → VersionStampStage → EmbeddingStage → IndexingStage → indexed

Splitting it this way makes each step observable, testable, and
replaceable: a future re-extraction pipeline is a new ``ExtractionStage``
behind the same seam, not a re-write.

INVARIANT: the four stages are the only path atoms take from raw
events to indexed vectors. M7 (H7) requires the embedder to be called
in a single batch so a failure is all-or-nothing — :class:`EmbeddingStage`
and :class:`IndexingStage` together enforce that.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Iterable

from .atom import MemoryAtom
from .embeddings import Embedder
from ..events.model import CaptureEvent
from .extract import Extractor
from .index import MemoryIndex
from .store import AtomStore
from .versioned import stamp_version_metadata

Clock = Callable[[], datetime]


class ExtractionStage:
    """Stage 1: events -> atom candidates.

    Calls the injected :class:`Extractor` on each event's text and
    produces one :class:`MemoryAtom` per extracted memory. The atom's
    ``start_ms`` and ``source_event_id`` come from the event; the
    ``created_at`` comes from the injected clock.
    """

    def __init__(self, extractor: Extractor, clock: Clock) -> None:
        self._extractor = extractor
        self._clock = clock

    def run(
        self, session_id: str, events: Iterable[CaptureEvent]
    ) -> list[MemoryAtom]:
        now = self._clock()
        atoms: list[MemoryAtom] = []
        for event in events:
            for index, memory in enumerate(self._extractor.extract(event.text)):
                atoms.append(
                    MemoryAtom(
                        atom_id=f"{event.event_id}:{index}",
                        session_id=session_id,
                        source_event_id=event.event_id,
                        kind=memory.kind,
                        text=memory.text,
                        created_at=now,
                        start_ms=event.start_ms,
                    )
                )
        return atoms


class VersionStampStage:
    """Stage 2: atoms -> stamped atoms.

    For v1, this is a no-op (the default atom already carries v1
    metadata). The seam exists so a future v2 entry point can construct
    new atoms carrying new version metadata and the supersedes link
    without changing any caller.
    """

    def run(self, atoms: Iterable[MemoryAtom]) -> list[MemoryAtom]:
        out: list[MemoryAtom] = []
        for atom in atoms:
            stamp_version_metadata(atom)
            out.append(atom)
        return out


class EmbeddingStage:
    """Stage 3: atoms -> (atom, vector) pairs.

    Embeds all atoms in a single batch so a mid-batch failure is
    visible to the caller as a single exception (M7). Returns
    ``(atom, vector)`` tuples in input order.
    """

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder

    def run(
        self, atoms: list[MemoryAtom]
    ) -> list[tuple[MemoryAtom, list[float]]]:
        if not atoms:
            return []
        vectors = self._embedder.embed([a.text for a in atoms])
        return list(zip(atoms, vectors))


class IndexingStage:
    """Stage 4: (atom, vector) pairs -> indexed atoms.

    Adds every (atom, vector) pair to the injected index. The index is
    idempotent on ``atom_id`` (a second pass is a no-op for already-
    indexed atoms), so this stage is safe to re-run.
    """

    def __init__(self, index: MemoryIndex) -> None:
        self._index = index

    def run(
        self, pairs: list[tuple[MemoryAtom, list[float]]]
    ) -> list[MemoryAtom]:
        added: list[MemoryAtom] = []
        for atom, vector in pairs:
            if self._index.add(atom, vector):
                added.append(atom)
        return added


class Pipeline:
    """The composition of the four stages with the :class:`AtomStore`.

    The :class:`Pipeline` is the only seam M4.3 (the extraction worker)
    needs to know about: it accepts events, runs the four stages,
    persists the resulting atoms, and returns the indexed ones. The
    cursor advancement is the worker's responsibility (H7), not the
    pipeline's.
    """

    def __init__(
        self,
        extraction: ExtractionStage,
        version_stamp: VersionStampStage,
        embedding: EmbeddingStage,
        indexing: IndexingStage,
        store: AtomStore,
    ) -> None:
        self._extraction = extraction
        self._version_stamp = version_stamp
        self._embedding = embedding
        self._indexing = indexing
        self._store = store

    def run(
        self, session_id: str, events: list[CaptureEvent]
    ) -> list[MemoryAtom]:
        """Run the four stages and persist the resulting atoms.

        Returns the atoms that were newly indexed. Raises if the
        embedder fails (M7): the index and atom store are unchanged in
        that case. Persistence happens AFTER successful indexing so the
        H7 cursor-after-indexing invariant holds end-to-end (no partial
        state survives a failure).
        """
        atoms = self._extraction.run(session_id, events)
        if not atoms:
            return []
        stamped = self._version_stamp.run(atoms)
        # Embed first. If this raises, neither the index nor the atom
        # store is touched (M7 / H7).
        pairs = self._embedding.run(stamped)
        indexed = self._indexing.run(pairs)
        # Persist only after the index has accepted the atoms. The
        # store's ``append`` is idempotent on ``atom_id``, so a retry
        # of the same session is safe.
        for atom in indexed:
            self._store.append(atom)
        return indexed
