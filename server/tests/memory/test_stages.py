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


def _event(seq: int, session_id: str = "s1", text: str = "hello") -> CaptureEvent:
    return CaptureEvent(
        event_id=f"{session_id}:{seq}",
        session_id=session_id,
        seq=seq,
        kind="transcript",
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        text=text,
        duration_ms=1000,
        start_ms=seq * 1000,
    )


# --- stage 1: extraction ------------------------------------------------------


def test_extraction_stage_produces_atoms_per_event():
    def clock() -> datetime:
        return datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
    stage = ExtractionStage(extractor=FixedExtractor(), clock=clock)
    atoms = stage.run("s1", [_event(0), _event(1)])
    assert len(atoms) == 2
    assert all(isinstance(a, MemoryAtom) for a in atoms)
    assert atoms[0].source_event_id == "s1:0"
    assert atoms[1].source_event_id == "s1:1"


def test_extraction_stage_respects_event_count():
    """An event that yields no memories still produces no atom (and that's fine)."""
    def clock() -> datetime:
        return datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
    stage = ExtractionStage(
        extractor=FixedExtractor(memories=[ExtractedMemory(kind="fact", text="only this")]),
        clock=clock,
    )
    atoms = stage.run("s1", [_event(0, text="ignored"), _event(1, text="ignored")])
    assert len(atoms) == 2  # one memory per event by default; both same content


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
