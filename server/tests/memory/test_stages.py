"""Tests for the four explicit pipeline stages (M4.2).

The :class:`ExtractionPipeline` is decomposed into four named stages,
each a pure function over the prior stage's output:

  1. :class:`ExtractionStage`     — events + extractor -> raw atom candidates
  2. :class:`VersionStampStage`   — atoms + version metadata -> stamped atoms
  3. :class:`EmbeddingStage`       — atoms + embedder        -> (atom, vector) pairs
  4. :class:`IndexingStage`        — (atom, vector) pairs + index -> indexed

Each stage can be unit-tested in isolation; the :class:`Pipeline` class
composes them. The contract is the same as the existing
:class:`ExtractionPipeline` run + :class:`IndexingPipeline`
``index_session`` sequence, but with the four responsibilities
visible at the call site.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

import pytest

from sense_server.memory.atom import MemoryAtom
from sense_server.memory.embeddings import Embedder
from sense_server.events.model import CaptureEvent
from sense_server.memory.index import InMemoryMemoryIndex
from sense_server.memory.stages import (
    EmbeddingStage,
    ExtractionStage,
    IndexingStage,
    Pipeline,
    VersionStampStage,
)
from sense_server.memory.extract import ExtractedMemory, Extractor
from sense_server.memory.store import InMemoryAtomStore


# --- helpers -----------------------------------------------------------------


class FixedExtractor:
    """Deterministic extractor: emit one memory per event, no LLM."""

    def __init__(self, memories: list[ExtractedMemory] | None = None) -> None:
        self._memories = memories

    def extract(self, text: str) -> list[ExtractedMemory]:
        if self._memories is not None:
            return list(self._memories)
        return [ExtractedMemory(kind="fact", text=text)]


class FixedEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t))] + [0.0, 0.0] for t in texts]


def _event(
    seq: int, session_id: str = "s1", text: str = "hello",
    speaker: str | None = None, speaker_confidence: float | None = None,
    speaker_assignment: str | None = None,
) -> CaptureEvent:
    return CaptureEvent(
        event_id=f"{session_id}:{seq}",
        session_id=session_id,
        seq=seq,
        kind="transcript",
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        text=text,
        duration_ms=1000,
        start_ms=seq * 1000,
        speaker=speaker,
        speaker_confidence=speaker_confidence,
        speaker_assignment=speaker_assignment,
    )


# --- stage 1: extraction ------------------------------------------------------

FIXED = datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)


def test_extraction_stage_joins_same_window_events_into_one_extract_call():
    """Events within one 60s window are joined into a single transcript and
    the extractor is called once. Atoms are attributed to the window's last
    event. This is the fix for per-event extraction on 1-second fragments,
    where the LLM saw each hop in isolation and returned []."""
    stage = ExtractionStage(extractor=FixedExtractor(), clock=lambda: FIXED)
    atoms = stage.run("s1", [_event(0, text="so basically"), _event(1, text="I was saying")])
    # one window -> one extract call -> one atom (FixedExtractor echoes joined text)
    assert len(atoms) == 1
    assert atoms[0].text == "so basically I was saying"
    assert atoms[0].source_event_id == "s1:1"  # window's last event
    assert atoms[0].start_ms == 1000
    assert atoms[0].atom_id == "s1:1:0"  # deterministic


def test_extraction_stage_splits_events_across_windows():
    """Events whose start_ms spans past window_ms from the first event start
    a new window, each producing its own extract call."""
    # window_ms=2000: event 0 (0ms), 1 (1000ms) -> W1; event 2 (2000ms) -> W2
    stage = ExtractionStage(extractor=FixedExtractor(), clock=lambda: FIXED, window_ms=2000)
    atoms = stage.run("s1", [_event(0, text="a"), _event(1, text="b"), _event(2, text="c")])
    assert [a.text for a in atoms] == ["a b", "c"]
    assert [a.source_event_id for a in atoms] == ["s1:1", "s1:2"]
    assert [a.atom_id for a in atoms] == ["s1:1:0", "s1:2:0"]


def test_extraction_stage_window_ms_default_is_60s():
    assert ExtractionStage(extractor=FixedExtractor(), clock=lambda: FIXED)._window_ms == 60_000


def test_extraction_stage_rejects_nonpositive_window_ms():
    with pytest.raises(ValueError):
        ExtractionStage(extractor=FixedExtractor(), clock=lambda: FIXED, window_ms=0)


def test_extraction_stage_empty_events_returns_empty():
    stage = ExtractionStage(extractor=FixedExtractor(), clock=lambda: FIXED)
    assert stage.run("s1", []) == []


def test_extraction_stage_window_yielding_no_memories_returns_no_atoms():
    """A window whose joined text yields [] produces no atom. The worker
    still advances the cursor past it; the stage just returns fewer atoms."""
    stage = ExtractionStage(extractor=FixedExtractor(memories=[]), clock=lambda: FIXED)
    assert stage.run("s1", [_event(0, text="noise"), _event(1, text="noise")]) == []


def test_extraction_stage_multiple_memories_in_a_window_indexed_by_position():
    """Two memories from one window get atom_ids {last}:{0} and {last}:{1}."""
    stage = ExtractionStage(
        extractor=FixedExtractor(memories=[
            ExtractedMemory(kind="fact", text="one"),
            ExtractedMemory(kind="task", text="two"),
        ]),
        clock=lambda: FIXED,
    )
    atoms = stage.run("s1", [_event(0, text="x"), _event(1, text="y")])
    assert [a.atom_id for a in atoms] == ["s1:1:0", "s1:1:1"]
    assert [a.kind for a in atoms] == ["fact", "task"]


# --- stage 1: live-path window hold-back -------------------------------------
#
# The gateway enqueues the session for extraction after EVERY transcript
# event, and the worker's process_session advances the cursor past every
# batch. On the live path each batch is therefore only the *new* events
# since the last call — a ~1s fragment — so the 60s windowing never sees a
# full window and the LLM is called on fragments (burning tokens, returning
# [], never forming memories). The fix: in live mode (finalize=False) the
# stage extracts only *closed* windows (a subsequent event already started a
# new window) and holds the trailing still-growing window back, reporting
# consumed_seq so the worker advances the cursor only past closed windows.


def test_extraction_stage_live_holds_back_single_growing_window():
    """In live mode a single still-growing window is NOT extracted — the
    LLM is not called on a fragment. consumed_seq is None so the worker
    leaves the cursor where it is and the events are re-seen next call."""
    calls: list[str] = []

    class Tracking(FixedExtractor):
        def extract(self, text: str) -> list[ExtractedMemory]:
            calls.append(text)
            return super().extract(text)

    stage = ExtractionStage(extractor=Tracking(), clock=lambda: FIXED, window_ms=60_000)
    events = [_event(0, text="a"), _event(1, text="b")]  # both within 60s -> one trailing window
    atoms, consumed = stage.extract("s1", events, finalize=False)
    assert atoms == []
    assert calls == []  # LLM not called on the fragment
    assert consumed is None  # nothing safe to advance past


def test_extraction_stage_finalize_extracts_trailing_window():
    """finalize=True (the reconcile/batch path) extracts the trailing
    window too, even though no further event will close it. consumed_seq
    reaches the last event."""
    stage = ExtractionStage(extractor=FixedExtractor(), clock=lambda: FIXED)
    events = [_event(0, text="a"), _event(1, text="b")]
    atoms, consumed = stage.extract("s1", events, finalize=True)
    assert [a.text for a in atoms] == ["a b"]
    assert consumed == 1


def test_extraction_stage_live_extracts_closed_window_holds_new_trailing():
    """When an event closes a window and starts a new one, live mode
    extracts the closed window and holds the new trailing window back.
    consumed_seq is the last event of the closed window only."""
    # window_ms=2000: e0(0ms),e1(1000ms) -> W1; e2(2000ms) -> W2 (trailing)
    stage = ExtractionStage(extractor=FixedExtractor(), clock=lambda: FIXED, window_ms=2000)
    events = [_event(0, text="a"), _event(1, text="b"), _event(2, text="c")]
    atoms, consumed = stage.extract("s1", events, finalize=False)
    assert [a.text for a in atoms] == ["a b"]  # W1 extracted
    assert consumed == 1  # last seq of W1; W2 (e2) held back


def test_extraction_stage_consumed_seq_helper():
    """consumed_seq() mirrors which windows extract() would consume, so
    the worker can compute the safe cursor without re-running the LLM."""
    stage = ExtractionStage(extractor=FixedExtractor(), clock=lambda: FIXED, window_ms=2000)
    assert stage.consumed_seq([], finalize=False) is None
    # one growing window: held back in live mode
    assert stage.consumed_seq([_event(0), _event(1)], finalize=False) is None
    # closed window present: advance to its last seq
    assert stage.consumed_seq([_event(0), _event(1), _event(2)], finalize=False) == 1
    # finalize mode: trailing window counts
    assert stage.consumed_seq([_event(0), _event(1)], finalize=True) == 1


def test_extraction_stage_run_is_backcompat_finalize_true_wrapper():
    """run() (used by existing batch tests) keeps returning just atoms and
    defaults to finalize=True so the trailing window is extracted."""
    stage = ExtractionStage(extractor=FixedExtractor(), clock=lambda: FIXED)
    atoms = stage.run("s1", [_event(0, text="a"), _event(1, text="b")])
    assert [a.text for a in atoms] == ["a b"]


# --- stage 2: version-stamp ---------------------------------------------------


def test_version_stamp_stage_is_noop_for_v1_atom():
    a = MemoryAtom(
        atom_id="a1", session_id="s1", source_event_id="e1", kind="fact", text="x",
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc), start_ms=0,
    )
    out = VersionStampStage().run([a])
    assert out == [a]
    assert out[0].extraction_version == "v1"


# --- stage 3: embedding -------------------------------------------------------


def test_embedding_stage_pairs_atoms_with_vectors():
    atoms = [
        MemoryAtom(
            atom_id="a1", session_id="s1", source_event_id="e1", kind="fact", text="x",
            created_at=datetime(2026, 7, 7, tzinfo=timezone.utc), start_ms=0,
        ),
        MemoryAtom(
            atom_id="a2", session_id="s1", source_event_id="e2", kind="fact", text="yy",
            created_at=datetime(2026, 7, 7, tzinfo=timezone.utc), start_ms=1,
        ),
    ]
    pairs = EmbeddingStage(embedder=FixedEmbedder()).run(atoms)
    assert len(pairs) == 2
    assert pairs[0][0].atom_id == "a1"
    assert pairs[0][1] == [1.0, 0.0, 0.0]   # len("x") == 1
    assert pairs[1][1] == [2.0, 0.0, 0.0]   # len("yy") == 2


# --- stage 4: indexing --------------------------------------------------------


def test_indexing_stage_adds_to_index():
    idx = InMemoryMemoryIndex()
    atoms = [
        MemoryAtom(
            atom_id="a1", session_id="s1", source_event_id="e1", kind="fact", text="x",
            created_at=datetime(2026, 7, 7, tzinfo=timezone.utc), start_ms=0,
        )
    ]
    pairs = [(atoms[0], [1.0, 0.0, 0.0])]
    added = IndexingStage(index=idx).run(pairs)
    assert added == atoms
    assert idx.has("a1")


# --- pipeline composition -----------------------------------------------------


def test_pipeline_full_run_stores_and_indexes_atoms():
    def clock() -> datetime:
        return datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
    store = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    pipe = Pipeline(
        extraction=ExtractionStage(extractor=FixedExtractor(), clock=clock),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=FixedEmbedder()),
        indexing=IndexingStage(index=idx),
        store=store,
    )
    pipe.run("s1", [_event(0), _event(1)])
    assert store.atoms("s1") != []
    assert idx.search("s1", [1.0, 0.0, 0.0], 10) != []


def test_pipeline_run_propagates_embedder_error_atomically():
    """M7: if the embedder raises mid-batch, no atoms are indexed."""
    def clock() -> datetime:
        return datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
    store = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()

    class Boom:
        def embed(self, texts):
            raise RuntimeError("boom")

    pipe = Pipeline(
        extraction=ExtractionStage(extractor=FixedExtractor(), clock=clock),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=Boom()),
        indexing=IndexingStage(index=idx),
        store=store,
    )
    with pytest.raises(RuntimeError, match="boom"):
        pipe.run("s1", [_event(0)])
    assert idx.search("s1", [1.0, 0.0, 0.0], 10) == []


# --- speaker attribution -----------------------------------------------------


def _stage():
    return ExtractionStage(FixedExtractor(), clock=lambda: FIXED, window_ms=60_000)


def test_atom_inherits_majority_speaker_of_its_window():
    events = [
        _event(0, text="a", speaker="you", speaker_confidence=0.9, speaker_assignment="confirmed"),
        _event(1, text="b", speaker="you", speaker_confidence=0.9, speaker_assignment="confirmed"),
        _event(2, text="c", speaker="you", speaker_confidence=0.9, speaker_assignment="confirmed"),
        _event(3, text="d", speaker="sarah", speaker_confidence=0.6, speaker_assignment="tentative"),
    ]
    atoms, _ = _stage().extract("s1", events, finalize=True)
    assert atoms
    assert atoms[0].speaker == "you"
    assert atoms[0].speaker_assignment == "confirmed"
    assert atoms[0].speaker_confidence is not None and atoms[0].speaker_confidence > 0.5


def test_atom_speaker_none_when_no_majority():
    events = [
        _event(0, text="a", speaker="you", speaker_confidence=0.8, speaker_assignment="confirmed"),
        _event(1, text="b", speaker="sarah", speaker_confidence=0.8, speaker_assignment="confirmed"),
    ]
    atoms, _ = _stage().extract("s1", events, finalize=True)
    assert atoms and atoms[0].speaker is None


def test_atom_speaker_none_when_all_events_unattributed():
    events = [_event(0, text="a"), _event(1, text="b")]
    atoms, _ = _stage().extract("s1", events, finalize=True)
    assert atoms and atoms[0].speaker is None
