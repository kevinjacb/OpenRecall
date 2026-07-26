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

import logging
from datetime import datetime
from typing import Callable, Iterable

from .atom import MemoryAtom
from .embeddings import Embedder
from ..events.model import CaptureEvent
from .extract import Extractor
from .index import MemoryIndex
from .store import AtomStore
from .versioned import stamp_version_metadata


def _majority_speaker(window: list[CaptureEvent]) -> "SpeakerAssignment | None":
    """The majority speaker by turn-weighted confidence, or None if no majority.

    Weight each attributed event by its confidence; the speaker with >50% of the
    total weight wins. Ties / no majority -> None (atom unattributed). Events
    with no speaker or assignment == "none" are skipped.
    """
    weights: dict[str, float] = {}
    assignment: dict[str, str] = {}
    for e in window:
        if not e.speaker or e.speaker_assignment == "none":
            continue
        w = e.speaker_confidence if e.speaker_confidence is not None else 0.0
        weights[e.speaker] = weights.get(e.speaker, 0.0) + w
        assignment[e.speaker] = e.speaker_assignment
    if not weights:
        return None
    total = sum(weights.values())
    if total <= 0:
        return None
    leader_id, leader_w = max(weights.items(), key=lambda kv: kv[1])
    if leader_w <= total / 2:  # no strict majority
        return None
    from ..ingest.speaker_identifier import SpeakerAssignment

    return SpeakerAssignment(leader_id, leader_w / total, assignment[leader_id])

log = logging.getLogger(__name__)

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
        *,
        version: str = "v1",
    ) -> None:
        if window_ms <= 0:
            raise ValueError("window_ms must be > 0")
        self._extractor = extractor
        self._clock = clock
        self._window_ms = window_ms
        # Identity of the extraction algorithm + prompt. The worker stamps the
        # per-session cursor with this; bumping it invalidates every existing
        # cursor so the new extractor re-processes past events instead of
        # silently skipping them (the "stale cursor locks out historical
        # transcripts" incident). Bump when the windowing, the prompt, or the
        # extractor model changes in a way that should re-form memories.
        self._version = version

    @property
    def version(self) -> str:
        """The extraction algorithm+prompt version this stage runs."""
        return self._version

    def run(
        self, session_id: str, events: Iterable[CaptureEvent], *,
        finalize: bool = True,
    ) -> list[MemoryAtom]:
        """Extract atoms from events. Back-compat batch entry point: returns
        just the atoms, defaults to ``finalize=True`` so the trailing window
        is extracted (the reconcile / batch behaviour the existing tests
        pin). For the live path use :meth:`extract` to also get the
        ``consumed_seq`` the cursor may safely advance to.
        """
        return self.extract(session_id, events, finalize=finalize)[0]

    def extract(
        self, session_id: str, events: Iterable[CaptureEvent], *,
        finalize: bool = True,
    ) -> "tuple[list[MemoryAtom], int | None]":
        """Extract atoms and report how far the cursor may safely advance.

        Returns ``(atoms, consumed_seq)`` where ``consumed_seq`` is the ``seq``
        of the last event of the last window that was actually extracted, or
        ``None`` if no window was extracted (so the caller leaves the cursor
        where it is and the events are re-seen next call).

        ``finalize`` selects the live vs. reconcile/batch policy:

        * ``finalize=True`` (reconcile-on-start, batch scripts): every window
          is extracted, including a trailing partial window — a short, ended
          session still forms memories. ``consumed_seq`` reaches the last event.
        * ``finalize=False`` (the gateway's live drain): only *closed*
          windows are extracted — a window is closed once a subsequent event
          has started a new window. The trailing still-growing window is held
          back: the gateway enqueues after every transcript event, so each
          live ``process_session`` batch would otherwise be only the new
          events since the last call (a ~1s fragment). The cursor advancing
          past every fragment meant the 60s windowing never saw a full window
          on the live path — the LLM was called on fragments, returned ``[]``,
          burned tokens, and never formed memories (the "events stop
          generating but the terminal shows tokens being used" incident).
          Holding the trailing window back makes the LLM fire once per
          closed ~``window_ms`` slice and advances the cursor only past
          closed windows.
        """
        now = self._clock()
        event_list = list(events)
        all_windows = self._windows(event_list)
        extracted_windows = self._extracted_windows(event_list, finalize=finalize)
        # Log the windowing decision so the live-vs-hold-back behaviour is
        # visible: how many windows the events form, how many are extracted
        # (closed) vs held back (the trailing growing window in live mode),
        # and how far the cursor may advance (consumed_seq).
        held = len(all_windows) - len(extracted_windows)
        log.info(
            "extraction_windows session=%s events=%d windows=%d "
            "extracted=%d held=%d finalize=%s",
            session_id, len(event_list), len(all_windows),
            len(extracted_windows), held, finalize,
        )
        if held:
            log.debug(
                "extraction_windows held back trailing window (live path); "
                "cursor will not advance past it until a new event closes it"
            )
        atoms: list[MemoryAtom] = []
        consumed_seq: int | None = None
        for window in extracted_windows:
            joined = " ".join(e.text for e in window)
            last = window[-1]
            majority = _majority_speaker(window)
            log.debug(
                "extraction_window_extract session=%s window_events=%d "
                "span_ms=%d seq_range=[%d,%d] joined=%r",
                session_id, len(window), last.start_ms - window[0].start_ms,
                window[0].seq, last.seq, joined[:200],
            )
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
                        speaker=(majority.speaker_id if majority else None),
                        speaker_confidence=(majority.confidence if majority else None),
                        speaker_assignment=(majority.assignment if majority else None),
                    )
                )
            consumed_seq = last.seq
        log.info(
            "extraction_stage_done session=%s atoms=%d consumed_seq=%s",
            session_id, len(atoms), consumed_seq,
        )
        return atoms, consumed_seq

    def consumed_seq(
        self, events: Iterable[CaptureEvent], *, finalize: bool = True,
    ) -> "int | None":
        """The ``seq`` of the last event of the last window :meth:`extract`
        would consume — i.e. how far the cursor may safely advance. ``None``
        if no window would be extracted (the live path with only a growing
        trailing window). Mirrors :meth:`extract` without running the LLM, so
        the worker can compute the safe cursor independently of the (raising)
        pipeline call for the dead-letter path.
        """
        windows = self._extracted_windows(list(events), finalize=finalize)
        return windows[-1][-1].seq if windows else None

    def _extracted_windows(
        self, events: list[CaptureEvent], *, finalize: bool,
    ) -> list[list[CaptureEvent]]:
        """The windows :meth:`extract` will call the LLM on.

        All windows except the trailing one are *closed* — a subsequent event
        already started a new window, so no future event can join them. The
        trailing window is the only one that may still grow. In live mode
        (``finalize=False``) it is held back; in reconcile/batch mode
        (``finalize=True``) it is extracted too.
        """
        windows = self._windows(events)
        if not windows:
            return []
        if finalize:
            return windows
        # Live mode: hold the still-growing trailing window back.
        return windows[:-1]

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

    @property
    def version(self) -> str:
        """The extraction version this pipeline runs (from its ExtractionStage).

        The worker stamps the per-session cursor with this so an algorithm or
        prompt change invalidates old cursors and re-extracts historical data.
        """
        return self._extraction.version

    def run(
        self, session_id: str, events: list[CaptureEvent], *,
        finalize: bool = True,
    ) -> list[MemoryAtom]:
        """Run the four stages and persist the resulting atoms.

        Returns the atoms that were newly indexed. Raises if the
        embedder fails (M7): the index and atom store are unchanged in
        that case. Persistence happens AFTER successful indexing so the
        H7 cursor-after-indexing invariant holds end-to-end (no partial
        state survives a failure).

        ``finalize`` is forwarded to :meth:`ExtractionStage.run`: ``True``
        (the default, used by reconcile-on-start and the batch scripts) also
        extracts the trailing partial window; ``False`` (the gateway's live
        drain) holds the still-growing trailing window back so the LLM is
        not called on a fragment. The worker reads :meth:`consumed_seq` to
        advance the cursor only past what was actually extracted.
        """
        atoms = self._extraction.run(session_id, events, finalize=finalize)
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

    def consumed_seq(
        self, events: list[CaptureEvent], *, finalize: bool = True,
    ) -> "int | None":
        """Forward to :meth:`ExtractionStage.consumed_seq`: the seq the
        worker may safely advance the cursor to, or ``None`` to hold back."""
        return self._extraction.consumed_seq(events, finalize=finalize)
