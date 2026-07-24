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

    Groups ordered events into time windows (at most ``window_ms`` of span
    from the first event's ``start_ms``), joins each window's text into one
    transcript, and calls the extractor **once per window** — not once per
    event. The streaming transcriber emits one transcript per 1-second audio
    hop, so per-event extraction fed the LLM fragments in isolation
    ("and send it to my phone now.") and it returned ``[]`` for nearly every
    hop; memories that span multiple hops (the common case) never formed.
    Windowing lets the model see a coherent ~60s slice of conversation.

    Every atom produced by a window is attributed to the window's **last**
    event: ``source_event_id`` and ``start_ms`` are the last event's, and
    ``atom_id`` is ``"{last.event_id}:{index}"``. This keeps ids deterministic
    for a given set of events + ``window_ms``, so :class:`AtomStore.append`
    (idempotent on ``atom_id``) and worker re-runs stay safe. The cursor /
    H7 invariant is unchanged — the worker still advances the cursor to the
    last processed event seq only after extraction + indexing succeed.
    """

    def __init__(
        self, extractor: Extractor, clock: Clock, window_ms: int = 60_000,
    ) -> None:
        if window_ms <= 0:
            raise ValueError("window_ms must be > 0")
        self._extractor = extractor
        self._clock = clock
        self._window_ms = window_ms

    def run(
        self, session_id: str, events: Iterable[CaptureEvent]
    ) -> list[MemoryAtom]:
        now = self._clock()
        atoms: list[MemoryAtom] = []
        for window in self._windows(list(events)):
            joined = " ".join(e.text for e in window)
            last = window[-1]
            for index, memory in enumerate(self._extractor.extract(joined)):
                atoms.append(
                    MemoryAtom(
                        atom_id=f"{last.event_id}:{index}",
                        session_id=session_id,
                        source_event_id=last.event_id,
                        kind=memory.kind,
                        text=memory.text,
                        created_at=now,
                        start_ms=last.start_ms,
                    )
                )
        return atoms

    def _windows(
        self, events: list[CaptureEvent]
    ) -> list[list[CaptureEvent]]:
        """Group events ordered by ``start_ms`` into windows of at most
        ``window_ms`` span. A window starts at an event's ``start_ms`` and
        includes every following event whose ``start_ms`` is within
        ``window_ms`` of it; the next event past that boundary starts a new
        window. Silence gaps (no events for a stretch) simply shrink a
        window rather than forcing a split — only elapsed span from the
        window's first event matters, so a conversation starting mid-minute
        groups correctly instead of being cleaved at a wall-clock boundary.
        """
        windows: list[list[CaptureEvent]] = []
        current: list[CaptureEvent] = []
        window_start_ms: int | None = None
        for event in events:
            if (
                window_start_ms is None
                or event.start_ms - window_start_ms >= self._window_ms
            ):
                if current:
                    windows.append(current)
                current = [event]
                window_start_ms = event.start_ms
            else:
                current.append(event)
        if current:
            windows.append(current)
        return windows


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
