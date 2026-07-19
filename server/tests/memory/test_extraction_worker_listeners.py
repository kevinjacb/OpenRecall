"""Tests for the listener list on ExtractionWorker (P3 proactive trigger).

The worker dispatches a SessionCompletion to every registered listener
AFTER the per-session cursor advances. Listener failures must not block
the worker or other listeners (H7: cursor advances on success only;
listener exceptions are caught and counted).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from sense_server.agent.metrics import InMemoryMetricsRecorder
from sense_server.contracts.metrics import Metrics
from sense_server.events.model import CaptureEvent
from sense_server.events.store import InMemoryEventStore
from sense_server.memory.extract import ExtractedMemory
from sense_server.memory.extraction_worker import (
    ExtractionEnqueuer,
    ExtractionWorker,
    SessionCompletion,
)
from sense_server.memory.index import InMemoryMemoryIndex
from sense_server.memory.stages import (
    EmbeddingStage,
    ExtractionStage,
    IndexingStage,
    Pipeline,
    VersionStampStage,
)
from sense_server.memory.store import InMemoryAtomStore


class FixedExtractor:
    def extract(self, text: str) -> list[ExtractedMemory]:
        return [ExtractedMemory(kind="fact", text=text)]


class HappyEmbedder:
    def embed(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]


def _event(seq: int, session_id: str = "s1", text: str = "hello") -> CaptureEvent:
    return CaptureEvent(
        event_id=f"{session_id}:{seq}",
        session_id=session_id,
        seq=seq,
        kind="transcript",
        created_at=datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc),
        text=text,
        duration_ms=1000,
        start_ms=seq * 1000,
    )


def _build_worker(events, atoms, idx, metrics, enq):
    def clock() -> datetime:
        return datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
    pipeline = Pipeline(
        extraction=ExtractionStage(extractor=FixedExtractor(), clock=clock),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=HappyEmbedder()),
        indexing=IndexingStage(index=idx),
        store=atoms,
    )
    return ExtractionWorker(
        events=events, atoms=atoms, pipeline=pipeline, metrics=metrics, enqueuer=enq,
    )


async def _drain_listeners() -> None:
    """Yield several times so the listener tasks scheduled by
    process_session (and their done-callbacks) all run to completion."""
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_listener_invoked_after_cursor_advances():
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)
    worker = _build_worker(events, atoms, idx, metrics, enq)

    seen: list[SessionCompletion] = []

    async def listener(c: SessionCompletion) -> None:
        seen.append(c)
    worker.add_listener(listener)

    events.append(_event(0, "s1", "hello"))
    worker.process_session("s1")
    await _drain_listeners()
    assert len(seen) == 1
    assert seen[0].session_id == "s1"
    assert seen[0].event_id_range == (0, 0)


@pytest.mark.asyncio
async def test_listener_not_invoked_when_pipeline_fails():
    """H7: a pipeline failure must not advance the cursor, and the
    listener must not be invoked (the session hasn't actually completed)."""
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)

    class BrokenIndex:
        def add(self, atom, vector): raise RuntimeError("index down")
        def delete(self, atom_id): pass
        def query(self, *a, **kw): return []

    def clock() -> datetime:
        return datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
    pipeline = Pipeline(
        extraction=ExtractionStage(extractor=FixedExtractor(), clock=clock),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=HappyEmbedder()),
        indexing=IndexingStage(index=BrokenIndex()),
        store=atoms,
    )
    worker = ExtractionWorker(
        events=events, atoms=atoms, pipeline=pipeline, metrics=metrics, enqueuer=enq,
    )

    seen: list[SessionCompletion] = []

    async def listener(c: SessionCompletion) -> None:
        seen.append(c)
    worker.add_listener(listener)

    events.append(_event(0, "s1", "hello"))
    with pytest.raises(RuntimeError):
        worker.process_session("s1")
    await _drain_listeners()
    assert seen == []


@pytest.mark.asyncio
async def test_listener_exception_does_not_block_other_listeners():
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)
    worker = _build_worker(events, atoms, idx, metrics, enq)

    seen: list[str] = []

    async def bad_listener(c: SessionCompletion) -> None:
        raise RuntimeError("listener boom")

    async def good_listener(c: SessionCompletion) -> None:
        seen.append(c.session_id)
    worker.add_listener(bad_listener)
    worker.add_listener(good_listener)

    events.append(_event(0, "s1", "hello"))
    worker.process_session("s1")
    await _drain_listeners()
    assert seen == ["s1"]
    # The failure was counted (per session_id tag, matching the increment).
    total = sum(
        v for (n, _), v in metrics._counters.items()
        if n == Metrics.EXTRACTION_LISTENER_FAILURE_TOTAL
    )
    assert total == 1


@pytest.mark.asyncio
async def test_remove_listener_works():
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)
    worker = _build_worker(events, atoms, idx, metrics, enq)

    async def listener(c): pass
    worker.add_listener(listener)
    worker.remove_listener(listener)
    assert worker._listeners == []


@pytest.mark.asyncio
async def test_in_iteration_remove_is_safe():
    """A listener that calls remove_listener during dispatch must not
    cause the iteration to skip the next listener or raise."""
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)
    worker = _build_worker(events, atoms, idx, metrics, enq)

    seen: list[str] = []
    listener_to_remove = None

    async def bad(c: SessionCompletion) -> None:
        worker.remove_listener(listener_to_remove)

    async def good(c: SessionCompletion) -> None:
        seen.append(c.session_id)

    listener_to_remove = good
    worker.add_listener(bad)
    worker.add_listener(good)

    events.append(_event(0, "s1", "hello"))
    worker.process_session("s1")
    await _drain_listeners()
    assert seen == ["s1"]
