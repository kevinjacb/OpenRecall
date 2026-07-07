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

from sense_server.agent.metrics import InMemoryMetricsRecorder
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
    assert metrics.counter(Metrics_or := __import__("sense_server.contracts.metrics", fromlist=["Metrics"]).Metrics.EXTRACTION_QUEUE_OVERFLOW_TOTAL) == 2
