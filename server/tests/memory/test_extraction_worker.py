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
from sense_server.events.store import InMemoryEventStore, SqliteEventStore
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
from sense_server.memory.store import InMemoryAtomStore, SqliteAtomStore


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


@pytest.mark.asyncio
async def test_worker_start_reconciles_historical_events():
    """Reconcile-on-start: the worker must catch up on any session that
    has un-indexed events left over from before the worker was up. The
    live enqueuer only sees new events; without a startup sweep, a
    gateway that was offline for an hour leaves 60 minutes of un-indexed
    transcripts — and a fresh gateway start on a populated events.db
    leaves ALL history un-indexed."""
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    # Pre-seed two sessions with events that the live path never enqueued
    # (the gateway was down, or this is a fresh worker after a restart).
    events.append(_event(0, session_id="s1"))
    events.append(_event(1, session_id="s1"))
    events.append(_event(0, session_id="s2"))
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)
    w = _build_worker(events, atoms, idx, HappyEmbedder(), metrics=metrics)
    w.replace_enqueuer(enq)
    await w.start()
    # The startup sweep runs synchronously inside start(); we should
    # already see the cursors advanced without any enqueue() call.
    assert atoms.get_cursor("s1") == 1
    assert atoms.get_cursor("s2") == 0
    # And atoms should be in the store.
    assert len(atoms.atoms("s1")) == 2
    assert len(atoms.atoms("s2")) == 1
    await w.stop()


@pytest.mark.asyncio
async def test_worker_start_reconcile_is_idempotent_across_restart():
    """Re-running start() after a stop() must not re-extract atoms that
    are already at the cursor (H7 still holds)."""
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0, session_id="s1"))
    events.append(_event(1, session_id="s1"))
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)
    w = _build_worker(events, atoms, idx, HappyEmbedder(), metrics=metrics)
    w.replace_enqueuer(enq)
    await w.start()
    assert len(atoms.atoms("s1")) == 2
    await w.stop()
    # Spin up a fresh worker against the same stores — the cursor is
    # already at the latest seq, so reconcile() must produce no new atoms.
    enq2 = ExtractionEnqueuer(capacity=10, metrics=metrics)
    w2 = _build_worker(events, atoms, idx, HappyEmbedder(), metrics=metrics)
    w2.replace_enqueuer(enq2)
    await w2.start()
    assert len(atoms.atoms("s1")) == 2  # unchanged
    await w2.stop()


@pytest.mark.asyncio
async def test_worker_start_reconciles_against_sqlite_event_store(tmp_path):
    """Production path: SqliteEventStore + SqliteAtomStore. The reconcile
    sweep on start() must use ``SELECT DISTINCT session_id`` from the
    sqlite store (not the broken private-attribute introspection that
    pre-dated EventStore.sessions()). This is the gap that left 12
    historical sessions un-indexed on the live gateway.

    Without this test, the in-memory + sqlite divergence is silent:
    in-memory reconcile always works (because ``_by_session`` is
    visible), sqlite reconcile never works (because the old code
    couldn't see the rows). Catching that here means a future
    refactor of either store can't silently break the production path.
    """
    events_db = tmp_path / "events.db"
    atoms_db = tmp_path / "atoms.db"
    events = SqliteEventStore(events_db)
    atoms = SqliteAtomStore(atoms_db)
    idx = InMemoryMemoryIndex()

    # Pre-seed two sessions — this is the "gateway was up before the
    # worker started" scenario, the one that left the user's history
    # un-extracted in production.
    for seq in range(3):
        events.append(_event(seq, session_id="session-A", text=f"A{seq}"))
    for seq in range(2):
        events.append(_event(seq, session_id="session-B", text=f"B{seq}"))

    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)
    pipeline = Pipeline(
        extraction=ExtractionStage(extractor=FixedExtractor(), clock=lambda: datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=HappyEmbedder()),
        indexing=IndexingStage(index=idx),
        store=atoms,
    )
    w = ExtractionWorker(
        events=events, atoms=atoms, pipeline=pipeline,
        metrics=metrics, enqueuer=enq,
    )
    await w.start()
    try:
        # The reconcile sweep runs on a worker thread via
        # ``asyncio.to_thread`` so the event loop stays responsive.
        # Poll for the cursor advance (it should be effectively
        # immediate — a few events against an in-process embedder).
        for _ in range(40):
            if atoms.get_cursor("session-A") == 2 and atoms.get_cursor("session-B") == 1:
                break
            await asyncio.sleep(0.01)
        # Both historical sessions were swept.
        assert atoms.get_cursor("session-A") == 2
        assert atoms.get_cursor("session-B") == 1
        # And atoms are durable in the sqlite atom store.
        assert len(atoms.atoms("session-A")) == 3
        assert len(atoms.atoms("session-B")) == 2
        # The index was populated.
        assert idx.search("session-A", [1.0, 0.0, 0.0], 10) != []
    finally:
        await w.stop()

    # A second worker against the same DBs (the "after a restart" case):
    # reconcile is a no-op because cursors are already at the latest seq.
    events2 = SqliteEventStore(events_db)
    atoms2 = SqliteAtomStore(atoms_db)
    enq2 = ExtractionEnqueuer(capacity=10, metrics=metrics)
    pipeline2 = Pipeline(
        extraction=ExtractionStage(extractor=FixedExtractor(), clock=lambda: datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=HappyEmbedder()),
        indexing=IndexingStage(index=idx),
        store=atoms2,
    )
    w2 = ExtractionWorker(
        events=events2, atoms=atoms2, pipeline=pipeline2,
        metrics=metrics, enqueuer=enq2,
    )
    await w2.start()
    try:
        for _ in range(40):
            # Atoms shouldn't change — the second start should be a no-op.
            if atoms2.get_cursor("session-A") == 2:
                break
            await asyncio.sleep(0.01)
        assert len(atoms2.atoms("session-A")) == 3
        assert len(atoms2.atoms("session-B")) == 2
    finally:
        await w2.stop()
