"""Tests for the gateway wiring of ExtractionEnqueuer and ExtractionWorker.

The gateway (run_gateway.py) constructs the worker at startup, starts it,
and stops it on shutdown. The enqueuer is the only seam the hot path
needs to know about: every successful event append calls
``enqueuer.enqueue(session_id)``.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from opensapien_server.agent.metrics import InMemoryMetricsRecorder
from opensapien_server.events.model import CaptureEvent
from opensapien_server.events.store import InMemoryEventStore
from opensapien_server.gateway.core import GatewayCore
from opensapien_server.ingest.pipeline import Transcript
from opensapien_server.memory.atom import MemoryAtom
from opensapien_server.memory.embeddings import Embedder
from opensapien_server.memory.extract import ExtractedMemory, Extractor
from opensapien_server.memory.index import InMemoryMemoryIndex
from opensapien_server.memory.extraction_worker import (
    ExtractionEnqueuer,
    ExtractionWorker,
)
from opensapien_server.memory.stages import (
    EmbeddingStage,
    ExtractionStage,
    IndexingStage,
    Pipeline,
    VersionStampStage,
)
from opensapien_server.memory.store import InMemoryAtomStore


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


def _build_worker(events, atoms, idx, metrics=None):
    def clock() -> datetime:
        return datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)

    class FixedExtractor:
        def extract(self, text: str) -> list[ExtractedMemory]:
            return [ExtractedMemory(kind="fact", text=text)]

    class FixedEmbedder:
        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0, 0.0] for _ in texts]

    pipeline = Pipeline(
        extraction=ExtractionStage(extractor=FixedExtractor(), clock=clock),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=FixedEmbedder()),
        indexing=IndexingStage(index=idx),
        store=atoms,
    )
    return ExtractionWorker(
        events=events,
        atoms=atoms,
        pipeline=pipeline,
        metrics=metrics or InMemoryMetricsRecorder(),
    )


@pytest.mark.asyncio
async def test_enqueuer_drains_after_start():
    """The wiring is: enqueuer enqueues, worker drains after start()."""
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0, session_id="s1"))
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)
    w = _build_worker(events, atoms, idx, metrics=metrics)
    w.replace_enqueuer(enq)
    await w.start()
    enq.enqueue("s1")
    # wait briefly
    for _ in range(40):
        if atoms.get_cursor("s1") == 0:
            break
        await asyncio.sleep(0.01)
    assert atoms.get_cursor("s1") == 0
    await w.stop()


@pytest.mark.asyncio
async def test_enqueuer_overflow_with_small_capacity():
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=2, metrics=metrics)
    enq.enqueue("a")
    enq.enqueue("a")
    enq.enqueue("a")
    enq.enqueue("a")
    assert metrics.counter(Metrics_or := __import__("opensapien_server.contracts.metrics", fromlist=["Metrics"]).Metrics.EXTRACTION_QUEUE_OVERFLOW_TOTAL) == 2


@pytest.mark.asyncio
async def test_enqueue_finalize_extracts_trailing_window_without_restart():
    """Bug C: a session that ends with a trailing partial 60s window must
    have it extracted when the session ends — not held back until a server
    restart. The live path (``enqueue`` → ``finalize=False``) correctly holds
    the trailing growing window back; ``enqueue_finalize`` (driven by bye /
    connection close) feeds a ``finalize=True`` pass through the worker so the
    ended session forms memories immediately.

    The event is appended AFTER ``start()`` so the reconcile-on-start sweep
    (which runs ``finalize=True``) doesn't see it — the only thing that can
    extract it here is the finalize enqueue."""
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)
    w = _build_worker(events, atoms, idx, metrics=metrics)
    w.replace_enqueuer(enq)
    await w.start()  # reconcile-on-start: no events yet -> no-op

    # A single event arrives AFTER start, so reconcile didn't see it.
    events.append(_event(0, session_id="s1", text="I like oatmeal"))
    enq.enqueue("s1")  # live path: finalize=False -> trailing window held
    # Let the live drain run: cursor stays -1, no atoms indexed.
    for _ in range(20):
        await asyncio.sleep(0.01)
    assert atoms.get_cursor("s1") == -1
    assert idx.search("s1", [1.0, 0.0, 0.0], 10) == []

    # Session ends -> a finalize pass is enqueued.
    enq.enqueue_finalize("s1")
    for _ in range(40):
        if atoms.get_cursor("s1") == 0:
            break
        await asyncio.sleep(0.01)
    assert atoms.get_cursor("s1") == 0
    results = idx.search("s1", [1.0, 0.0, 0.0], 10)
    assert len(results) == 1
    assert results[0].atom.text == "I like oatmeal"
    await w.stop()


def test_gateway_core_enqueues_after_emit():
    """After _emit stores a transcript event, the enqueuer should see
    the session id. The pipeline factory and the audio hot path are
    bypassed: we drive _emit directly so this is a pure unit test of
    the enqueue side-effect.

    This is the binding seam for the "memory never gets extracted"
    bug fix: the hot path was writing events to the event store and
    the session index but never enqueueing the session for the
    background worker, so the atom table stayed empty.
    """
    events = InMemoryEventStore()
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)

    # The factory is never invoked (we don't call Hello), so a no-op
    # suffices. This keeps the test pure and avoids importing the
    # heavy Opus / whisper deps.
    noop_factory = lambda _start_seq: None  # noqa: E731
    core = GatewayCore(
        pipeline_factory=noop_factory,
        event_store=events,
        enqueuer=enq,  # NEW PARAM — this is the seam we're testing
    )
    # Set the bound state without going through Hello (we don't need
    # a real AudioIngestPipeline for this test).
    core._session_id = "s1"
    core._pipeline = None

    # Drive _emit with a fake transcript; the method only reads
    # self._session_id, self._store, self._session_index, and (after
    # this fix) self._enqueuer.
    core._emit([Transcript(text="hello world", duration_ms=1500)])

    assert enq.qsize() == 1, f"expected 1 enqueued, got {enq.qsize()}"
