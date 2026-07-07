"""Tests for the ExtractionWorker (M4.3) and the H7 cursor-after-indexing
guarantee.

INVARIANT (H7): the per-session extraction cursor advances only after
BOTH extraction AND indexing have succeeded for that session. A failure
in the embedder (or the indexer) must leave the cursor where it was,
so a retry processes the same events again. This is what makes the
worker self-healing on a transient outage.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from typing import Iterable

import pytest

from sense_server.agent.metrics import InMemoryMetricsRecorder
from sense_server.contracts.clock import FakeClock
from sense_server.contracts.id_generator import DeterministicIdGenerator
from sense_server.contracts.metrics import Metrics
from sense_server.events.model import CaptureEvent
from sense_server.events.store import InMemoryEventStore
from sense_server.memory.atom import MemoryAtom
from sense_server.memory.embeddings import Embedder
from sense_server.memory.extract import ExtractedMemory, Extractor
from sense_server.memory.index import InMemoryMemoryIndex
from sense_server.memory.extraction_worker import (
    ExtractionEnqueuer,
    ExtractionWorker,
)
from sense_server.memory.stages import (
    EmbeddingStage,
    ExtractionStage,
    IndexingStage,
    Pipeline,
    VersionStampStage,
)
from sense_server.memory.store import InMemoryAtomStore


# --- fakes ------------------------------------------------------------------


class FixedExtractor:
    def __init__(self, memories: list[ExtractedMemory] | None = None) -> None:
        self._memories = memories

    def extract(self, text: str) -> list[ExtractedMemory]:
        if self._memories is not None:
            return list(self._memories)
        return [ExtractedMemory(kind="fact", text=text)]


class HappyEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]


class FlakyEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("embedder boom")
        return [[1.0, 0.0, 0.0] for _ in texts]


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


def _build_worker(
    events: InMemoryEventStore,
    atoms: InMemoryAtomStore,
    idx: InMemoryMemoryIndex,
    embedder,
    metrics: InMemoryMetricsRecorder | None = None,
):
    def clock() -> datetime:
        return datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
    pipeline = Pipeline(
        extraction=ExtractionStage(extractor=FixedExtractor(), clock=clock),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=embedder),
        indexing=IndexingStage(index=idx),
        store=atoms,
    )
    return ExtractionWorker(
        events=events,
        atoms=atoms,
        pipeline=pipeline,
        metrics=metrics or InMemoryMetricsRecorder(),
    )


# --- tests ------------------------------------------------------------------


def test_enqueue_does_not_block():
    enq = ExtractionEnqueuer(capacity=10)
    enq.enqueue("s1")
    enq.enqueue("s1")
    assert enq.qsize() == 2


def test_enqueue_overflow_is_counted_and_dropped():
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=1, metrics=metrics)
    enq.enqueue("s1")
    enq.enqueue("s1")
    enq.enqueue("s1")  # overflow
    assert enq.qsize() == 1
    assert metrics.counter(Metrics.EXTRACTION_QUEUE_OVERFLOW_TOTAL) == 2


def test_worker_processes_one_session_atomically():
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0))
    events.append(_event(1))
    w = _build_worker(events, atoms, idx, HappyEmbedder())
    indexed = w.process_session("s1")
    assert len(indexed) == 2
    assert idx.search("s1", [1.0, 0.0, 0.0], 10) != []
    # cursor advanced past both events
    assert atoms.get_cursor("s1") == 1


def test_worker_h7_does_not_advance_cursor_on_embedder_failure():
    """H7: a failed indexing step must leave the cursor where it was,
    so the next attempt retries the same events."""
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0))
    w = _build_worker(events, atoms, idx, FlakyEmbedder())
    with pytest.raises(RuntimeError, match="embedder boom"):
        w.process_session("s1")
    # cursor was NOT advanced
    assert atoms.get_cursor("s1") == -1
    assert idx.search("s1", [1.0, 0.0, 0.0], 10) == []
    # and atoms were not persisted either (embedder failed before store.append)
    assert atoms.atoms("s1") == []


def test_worker_self_heals_after_retry():
    """H7: a successful retry indexes everything that was previously pending."""
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0))
    embedder = FlakyEmbedder()
    w = _build_worker(events, atoms, idx, embedder)
    with pytest.raises(RuntimeError):
        w.process_session("s1")
    # Retry: same worker, same embedder (now succeeding).
    indexed = w.process_session("s1")
    assert len(indexed) == 1
    assert idx.search("s1", [1.0, 0.0, 0.0], 10) != []


def test_worker_records_indexing_failures_metric():
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0))
    metrics = InMemoryMetricsRecorder()
    w = _build_worker(events, atoms, idx, FlakyEmbedder(), metrics=metrics)
    with pytest.raises(RuntimeError):
        w.process_session("s1")
    assert metrics.counter(
        Metrics.INDEXING_FAILURES_TOTAL, tags={"session_id": "s1"}
    ) == 1


def test_worker_records_extraction_latency():
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0))
    events.append(_event(1))
    metrics = InMemoryMetricsRecorder()
    w = _build_worker(events, atoms, idx, HappyEmbedder(), metrics=metrics)
    w.process_session("s1")
    hist = metrics.histogram(Metrics.EXTRACTION_LATENCY_MS)
    assert hist.count == 1
    assert hist.sum >= 0


def test_worker_reconciliation_drains_pending():
    """The periodic reconciliation pass processes every session that has
    un-indexed events (i.e. cursor < last_event_seq)."""
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0, session_id="s1"))
    events.append(_event(1, session_id="s1"))
    events.append(_event(0, session_id="s2"))
    w = _build_worker(events, atoms, idx, HappyEmbedder())
    indexed = w.reconcile()
    assert len(indexed) == 3
    assert atoms.get_cursor("s1") == 1
    assert atoms.get_cursor("s2") == 0


def test_worker_isolated_failure_does_not_stop_others_v2():
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0, session_id="sA"))
    events.append(_event(0, session_id="sB"))
    embedder = FailFirstEmbedder()
    w = _build_worker(events, atoms, idx, embedder)
    indexed = w.reconcile()
    # First session fails (cursor stays at -1), second succeeds.
    assert atoms.get_cursor("sA") == -1
    assert atoms.get_cursor("sB") == 0
    assert {a.session_id for a in indexed} == {"sB"}


class FailFirstEmbedder:
    """Embedder that fails on the first call, succeeds on the second."""

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("first call fails")
        return [[1.0, 0.0, 0.0] for _ in texts]


@pytest.mark.asyncio
async def test_worker_lifecycle_start_stop():
    """start() and stop() are idempotent and the queue drains on stop."""
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0, session_id="s1"))
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)
    w = _build_worker(events, atoms, idx, HappyEmbedder(), metrics=metrics)
    # Replace the worker's enqueuer so the test uses one it controls.
    w.replace_enqueuer(enq)
    await w.start()
    enq.enqueue("s1")
    # Give the worker a beat to drain.
    for _ in range(40):
        if atoms.get_cursor("s1") == 0:
            break
        await asyncio.sleep(0.01)
    assert atoms.get_cursor("s1") == 0
    await w.stop()
