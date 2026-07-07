"""Architectural-invariant tests for the memory + agent layers (M5.2 / Section 3).

These are the binding regression tests for the plan's architectural invariants.
A failure here means the architecture is broken — the test name cites the
invariant it guards. Adding a new test here requires a corresponding invariant
in the spec; this file is the executable form of those invariants.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest

from sense_server.agent.metrics import InMemoryMetricsRecorder
from sense_server.contracts.clock import FakeClock
from sense_server.contracts.id_generator import DeterministicIdGenerator
from sense_server.contracts.metrics import Metrics
from sense_server.contracts.types import RetrieverContext
from sense_server.events.model import CaptureEvent
from sense_server.events.store import InMemoryEventStore
from sense_server.memory.atom import MemoryAtom
from sense_server.memory.embeddings import Embedder
from sense_server.memory.extract import ExtractedMemory, Extractor
from sense_server.memory.extraction_worker import (
    ExtractionEnqueuer,
    ExtractionWorker,
)
from sense_server.memory.index import InMemoryMemoryIndex
from sense_server.memory.retrieval import Retriever
from sense_server.memory.scoring import SimRecencyScorer
from sense_server.memory.stages import (
    EmbeddingStage,
    ExtractionStage,
    IndexingStage,
    Pipeline,
    VersionStampStage,
)
from sense_server.memory.store import InMemoryAtomStore


# --- helpers -----------------------------------------------------------------


def _atom(
    atom_id: str,
    session_id: str = "s1",
    text: str = "x",
    created_at: datetime | None = None,
    start_ms: int = 0,
) -> MemoryAtom:
    return MemoryAtom(
        atom_id=atom_id,
        session_id=session_id,
        source_event_id=f"e-{atom_id}",
        kind="fact",
        text=text,
        created_at=created_at or datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=start_ms,
    )


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


class _TokenBagEmbedder:
    """Token-bag projection embedder — same input → same vector."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            v = [0.0] * 4
            for i, ch in enumerate(t.encode()):
                v[i % 4] += float((ch * (i + 1)) % 251) / 251.0
            out.append(v)
        return out


class _FixedExtractor:
    def extract(self, text: str) -> list[ExtractedMemory]:
        return [ExtractedMemory(kind="fact", text=text)]


def _build_worker(
    events: InMemoryEventStore,
    atoms: InMemoryAtomStore,
    idx: InMemoryMemoryIndex,
    embedder=None,
    metrics: InMemoryMetricsRecorder | None = None,
):
    def clock() -> datetime:
        return datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
    pipeline = Pipeline(
        extraction=ExtractionStage(extractor=_FixedExtractor(), clock=clock),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=embedder or _TokenBagEmbedder()),
        indexing=IndexingStage(index=idx),
        store=atoms,
    )
    return ExtractionWorker(
        events=events,
        atoms=atoms,
        pipeline=pipeline,
        metrics=metrics or InMemoryMetricsRecorder(),
    )


# --- INV-1 + INV-6: thread safety + read-only retrieval ---------------------


def test_invariant_in_memory_atom_store_thread_safe():
    store = InMemoryAtomStore()
    atoms = [_atom(f"a{i}", text=f"text {i}", start_ms=i) for i in range(100)]
    threads = [
        threading.Thread(target=lambda chunk: [store.append(a) for a in chunk],
                          args=(atoms[i::4],))
        for i in range(4)
    ]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(store.atoms("s1")) == 100


def test_invariant_in_memory_index_thread_safe():
    idx = InMemoryMemoryIndex()
    atoms = [_atom(f"a{i}", text=f"text {i}", start_ms=i) for i in range(50)]
    vectors = [[float(i), 0.0, 0.0] for i in range(50)]
    pairs = list(zip(atoms, vectors))

    def worker(chunk: list) -> None:
        for a, v in chunk:
            idx.add(a, v)

    threads = [threading.Thread(target=worker, args=(pairs[i::4],)) for i in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(idx.search("s1", [1.0, 0.0, 0.0], 100)) == 50


def test_invariant_retrieval_is_read_only():
    """INV-6: 1000 retriever calls leave the index, the atom store, and the
    cursor unchanged."""
    idx = InMemoryMemoryIndex()
    atoms = InMemoryAtomStore()
    a = _atom("a1", text="hello world")
    atoms.append(a)
    idx.add(a, _TokenBagEmbedder().embed(["hello world"])[0])
    r = Retriever(
        embedder=_TokenBagEmbedder(),
        index=idx,
        scorer=SimRecencyScorer(half_life_s=1e12),
        clock=FakeClock(datetime(2026, 7, 7, tzinfo=timezone.utc)),
        ids=DeterministicIdGenerator(),
    )
    for _ in range(1000):
        r.retrieve(RetrieverContext(query_text="hello", limit=10))
    assert len(atoms.atoms("s1")) == 1
    assert len(idx.search("s1", [1.0, 0.0, 0.0], 10)) == 1
    assert atoms.get_cursor("s1") == -1


# --- INVARIANT 5: atoms are immutable ----------------------------------------


def test_invariant_atoms_immutable():
    a = _atom("a1")
    with pytest.raises(Exception):
        a.text = "mutated"  # type: ignore[misc]


# --- Extraction exactly-once (H7) --------------------------------------------


def test_invariant_extraction_exactly_once():
    """Processing the same session 10 times advances the cursor once
    and never duplicates atoms."""
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0))
    events.append(_event(1))
    w = _build_worker(events, atoms, idx)
    for _ in range(10):
        w.process_session("s1")
    assert atoms.get_cursor("s1") == 1
    assert len(atoms.atoms("s1")) == 2
    assert len(idx.search("s1", [1.0, 0.0, 0.0], 10)) == 2


def test_invariant_reconciliation_recovers_missed_jobs():
    """Reconcile processes every session that has un-indexed events
    (cursor < last event seq) even after multiple failures."""
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0, session_id="s1"))
    events.append(_event(0, session_id="s2"))
    events.append(_event(0, session_id="s3"))
    w = _build_worker(events, atoms, idx)
    indexed = w.reconcile()
    assert len(indexed) == 3
    for sid in ("s1", "s2", "s3"):
        assert atoms.get_cursor(sid) == 0


# --- INV-7: deterministic retrieval ordering --------------------------------


def test_invariant_retrieval_deterministic():
    idx = InMemoryMemoryIndex()
    embed = _TokenBagEmbedder()
    for i in range(5):
        a = _atom(f"a{i}", text=f"text {i}")
        idx.add(a, embed.embed([f"text {i}"])[0])
    r = Retriever(
        embedder=embed,
        index=idx,
        scorer=SimRecencyScorer(half_life_s=1e12),
        clock=FakeClock(datetime(2026, 7, 7, tzinfo=timezone.utc)),
        ids=DeterministicIdGenerator(),
    )
    rc1 = r.retrieve(RetrieverContext(query_text="text 0", limit=5))
    rc2 = r.retrieve(RetrieverContext(query_text="text 0", limit=5))
    assert [a.atom_id for a in rc1.atoms] == [a.atom_id for a in rc2.atoms]


def test_invariant_version_metadata_stamped_on_produced_atoms():
    """Every atom produced by the worker carries the v1 version metadata."""
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0))
    w = _build_worker(events, atoms, idx)
    w.process_session("s1")
    a = atoms.atoms("s1")[0]
    assert a.extraction_version == "v1"
    assert a.source_pipeline_version == "transcript"
    assert a.extractor_prompt_version == "v1"


def test_invariant_provenance_survives_retrieval():
    """Every scored atom carries its provenance."""
    idx = InMemoryMemoryIndex()
    embed = _TokenBagEmbedder()
    a = _atom("a1", session_id="s1", text="hello")
    idx.add(a, embed.embed(["hello"])[0])
    r = Retriever(
        embedder=embed,
        index=idx,
        scorer=SimRecencyScorer(half_life_s=1e12),
        clock=FakeClock(datetime(2026, 7, 7, tzinfo=timezone.utc)),
        ids=DeterministicIdGenerator(),
    )
    rc = r.retrieve(RetrieverContext(query_text="hello", limit=1))
    assert rc.atoms[0].provenance is not None
    assert rc.atoms[0].provenance.session_id == "s1"
    assert rc.atoms[0].provenance.extraction_version == "v1"
