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
from sense_server.memory.extract import ExtractedMemory, Extractor, LLMParseError
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


# --- cross-thread enqueue: structural regression guard ---------------------
#
# The hot path (gateway WebSocket handler) calls
# `self._enqueuer.enqueue(session_id)` from a worker thread (the
# thread created by `asyncio.to_thread(handle_message, core, message)`
# in gateway/adapter.py). `asyncio.Queue.put_nowait` is documented
# as not thread-safe: it manipulates `_unfinished_tasks`,
# `_finished`, and the `_getters` deque without any locking or
# loop handoff. A producer on a non-loop thread races the consumer
# on the loop thread on every one of those compound operations.
#
# The fix: the enqueuer holds the running loop (captured by
# `bind_loop` from `worker.start()`), and the producer path uses
# `loop.call_soon_threadsafe(self._queue.put_nowait, session_id)`
# so the actual queue manipulation happens on the loop thread.
#
# The tests below pin the *structure* of that fix (the
# call_soon_threadsafe handoff exists) rather than try to
# reproduce the race, which is GIL-scheduling-dependent.


@pytest.mark.asyncio
async def test_enqueue_uses_call_soon_threadsafe_when_loop_is_bound():
    """Structural regression guard: with a loop bound, ``enqueue``
    must dispatch the put via ``loop.call_soon_threadsafe``, not
    call ``self._queue.put_nowait`` directly. We assert this by
    wrapping the loop's ``call_soon_threadsafe`` and confirming
    the producer path goes through it. If a future refactor
    regresses the call_soon_threadsafe handoff (e.g. someone
    "optimizes" by calling put_nowait directly on a non-loop
    thread), this test fails.

    The test is a structural pin, not a race reproduction. The
    asyncio.Queue thread-safety defect is documented in the
    asyncio docs and visible in CPython's source; reproducing it
    empirically is GIL-scheduling-dependent and not useful as a
    regression gate.
    """
    enq = ExtractionEnqueuer(capacity=10)
    loop = asyncio.get_running_loop()
    enq.bind_loop(loop)

    # Wrap the loop's call_soon_threadsafe so we can count the
    # handoffs. The wrapper must preserve the original signature
    # (callback, *args) so the enqueuer's call site is unchanged.
    handoff_count = 0
    real_call_soon_threadsafe = loop.call_soon_threadsafe

    def counting_call_soon_threadsafe(callback, *args):
        nonlocal handoff_count
        handoff_count += 1
        return real_call_soon_threadsafe(callback, *args)

    loop.call_soon_threadsafe = counting_call_soon_threadsafe
    try:
        # Call enqueue from the loop thread (still cross-thread
        # safe to call even from the loop thread once the
        # call_soon_threadsafe path is in place).
        enq.enqueue("s1")
        # Yield to the loop so the call_soon_threadsafe callback
        # actually runs.
        await asyncio.sleep(0)
    finally:
        loop.call_soon_threadsafe = real_call_soon_threadsafe

    assert handoff_count == 1, (
        "enqueue() did not go through loop.call_soon_threadsafe; "
        "the cross-thread producer path regressed to direct "
        "asyncio.Queue.put_nowait from a non-loop thread."
    )
    # And the item actually landed in the queue.
    assert enq.qsize() == 1


def test_enqueue_uses_call_soon_threadsafe_from_non_loop_thread():
    """Same structural pin, but called from a worker thread (the
    actual production path: `asyncio.to_thread(handle_message, ...)`
    in gateway/adapter.py). On a worker thread there is no
    get_running_loop(), so any code that does
    `asyncio.get_event_loop()` or `asyncio.get_running_loop()`
    would raise. The fix's contract: enqueue is callable from
    any thread when a loop is bound, and goes through
    call_soon_threadsafe.

    The test driver runs a small event loop, binds it to the
    enqueuer, spawns a worker thread that calls enqueue (without
    any get_running_loop() call — that would raise on a non-loop
    thread), then verifies the item landed in the queue. The
    worker thread does NOT touch the loop directly; the
    enqueuer's call_soon_threadsafe is the only loop handoff.
    """
    import threading as _t

    enq = ExtractionEnqueuer(capacity=10)
    producer_done = _t.Event()
    producer_error: list[BaseException] = []

    def producer() -> None:
        try:
            enq.enqueue("from-worker-thread")
        except BaseException as e:
            producer_error.append(e)
        finally:
            producer_done.set()

    async def driver() -> None:
        loop = asyncio.get_running_loop()
        enq.bind_loop(loop)
        t = _t.Thread(target=producer, daemon=True)
        t.start()
        # Wait for the producer to call enqueue and return. The
        # actual put_nowait runs on the loop thread via
        # call_soon_threadsafe; yield to let it land.
        await asyncio.get_running_loop().run_in_executor(
            None, producer_done.wait, 1.0
        )
        # Give the loop one more tick to process the call_soon_threadsafe.
        await asyncio.sleep(0)

    asyncio.run(driver())
    assert producer_error == [], (
        f"producer thread raised: {producer_error[0]!r}"
    )
    assert enq.qsize() == 1, (
        f"expected 1 item in queue after cross-thread enqueue, "
        f"got {enq.qsize()}"
    )


def test_enqueue_from_non_loop_thread_uses_call_soon_threadsafe():
    """Stronger version of the above: wrap the bound loop's
    call_soon_threadsafe and confirm the producer thread went
    through it exactly once per enqueue. This is the structural
    regression guard against the documented asyncio.Queue
    thread-safety defect.

    The counter increments for every call_soon_threadsafe (the
    counting wrapper doesn't filter), so we sample the counter
    around the producer's enqueue call: the delta is the
    enqueue-related handoff. (Other call_soon_threadsafe calls
    in the test, e.g. ``run_in_executor`` scheduling the
    producer_done.wait, are sampled out of the delta window.)
    """
    import threading as _t

    enq = ExtractionEnqueuer(capacity=10)
    handoff_count = 0
    handoff_lock = _t.Lock()
    producer_error: list[BaseException] = []
    producer_enqueued = _t.Event()

    def producer() -> None:
        try:
            with handoff_lock:
                before = handoff_count
            enq.enqueue("s1")
            with handoff_lock:
                after = handoff_count
            # The enqueue handoff is the only thing that happens
            # between `before` and `after` on this thread.
            assert after == before + 1, (
                f"enqueue did not call call_soon_threadsafe exactly "
                f"once; before={before}, after={after}"
            )
        except BaseException as e:
            producer_error.append(e)
        finally:
            producer_enqueued.set()

    async def driver() -> None:
        loop = asyncio.get_running_loop()
        enq.bind_loop(loop)
        real = loop.call_soon_threadsafe

        def counting(cb, *args):
            nonlocal handoff_count
            with handoff_lock:
                handoff_count += 1
            return real(cb, *args)

        loop.call_soon_threadsafe = counting
        try:
            t = _t.Thread(target=producer, daemon=True)
            t.start()
            # Wait for the producer to finish its enqueue + assert.
            await asyncio.get_running_loop().run_in_executor(
                None, producer_enqueued.wait, 1.0
            )
            # Let the queued put_nowait run on the loop thread.
            await asyncio.sleep(0)
        finally:
            loop.call_soon_threadsafe = real

    asyncio.run(driver())
    assert producer_error == [], (
        f"producer thread raised: {producer_error[0]!r}"
    )
    assert enq.qsize() == 1


def test_enqueue_falls_back_to_direct_put_when_no_loop_is_bound():
    """Without a bound loop, enqueue uses the direct put_nowait
    path. This preserves the existing test ergonomics (construct
    an enqueuer, call enqueue, assert qsize) for the basic
    enqueue/overflow tests, and is the documented "loop-thread
    only" contract for the no-loop path. The cross-thread
    contract requires bind_loop; this test pins that the
    no-loop fallback is a separate, documented code path.
    """
    enq = ExtractionEnqueuer(capacity=10)
    assert enq.bound_loop is None
    enq.enqueue("s1")
    enq.enqueue("s1")
    assert enq.qsize() == 2


def test_worker_start_binds_the_loop_to_the_enqueuer():
    """``ExtractionWorker.start()`` runs on the event loop. It
    must capture the running loop and bind it to its enqueuer so
    the cross-thread producer path (gateway/adapter.py:
    ``asyncio.to_thread(handle_message, core, message)``) is
    safe. The bind is the seam that makes the structural
    regression test above meaningful in production: without the
    bind, the enqueuer would silently fall back to the
    unsafe direct put_nowait path.
    """
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)
    w = _build_worker(events, atoms, idx, HappyEmbedder(), metrics=metrics)
    w.replace_enqueuer(enq)
    assert enq.bound_loop is None  # not bound yet

    async def driver() -> None:
        await w.start()
        try:
            assert enq.bound_loop is asyncio.get_running_loop()
        finally:
            await w.stop()

    asyncio.run(driver())


@pytest.mark.asyncio
async def test_enqueue_uses_call_soon_threadsafe_end_to_end_after_worker_start():
    """End-to-end structural pin: after worker.start(), the
    enqueuer has the loop bound, and a producer calling
    enqueue() goes through call_soon_threadsafe. This is the
    production path: gateway hot path → enqueue → loop drain
    → process_session.
    """
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)
    w = _build_worker(events, atoms, idx, HappyEmbedder(), metrics=metrics)
    w.replace_enqueuer(enq)
    await w.start()
    try:
        loop = asyncio.get_running_loop()
        handoff_count = 0
        real = loop.call_soon_threadsafe

        def counting(cb, *args):
            nonlocal handoff_count
            handoff_count += 1
            return real(cb, *args)

        loop.call_soon_threadsafe = counting
        try:
            enq.enqueue("s1")
            await asyncio.sleep(0)  # let the queued put_nowait run
        finally:
            loop.call_soon_threadsafe = real
        assert handoff_count == 1
        assert enq.qsize() == 1
    finally:
        await w.stop()


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


class MalformedExtractor:
    """Always emit a malformed reply so the strict parser raises LLMParseError.

    Used to pin the H7 invariant for parse failures specifically:
    a malformed LLM response must not advance the cursor.
    """

    def __init__(self, payload: str = '["event", "Went to the gym this morning."]') -> None:
        self._payload = payload

    def extract(self, text: str) -> list[ExtractedMemory]:
        # The real LLMExtractor raises LLMParseError from _parse when
        # the reply is malformed. We mirror that contract here so the
        # worker test exercises the same exception path.
        raise LLMParseError(f"malformed extraction reply: {self._payload!r}")


def test_worker_does_not_advance_cursor_on_parse_failure():
    """H7 (parse-failure branch): a malformed LLM reply must leave the
    cursor where it was, so the next enqueue / reconciliation retries
    the same events. This is the bug that caused the live 0-atom
    incident: the parser silently returned [] and the worker advanced
    the cursor past events that were never extracted.
    """
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0))
    events.append(_event(1))

    def clock() -> datetime:
        return datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
    pipeline = Pipeline(
        extraction=ExtractionStage(extractor=MalformedExtractor(), clock=clock),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=HappyEmbedder()),
        indexing=IndexingStage(index=idx),
        store=atoms,
    )
    w = ExtractionWorker(
        events=events, atoms=atoms, pipeline=pipeline,
        metrics=InMemoryMetricsRecorder(),
    )

    with pytest.raises(LLMParseError):
        w.process_session("s1")

    # Cursor was NOT advanced: next retry sees the same events.
    assert atoms.get_cursor("s1") == -1
    # Nothing was indexed or persisted.
    assert idx.search("s1", [1.0, 0.0, 0.0], 10) == []
    assert atoms.atoms("s1") == []


def test_worker_increments_llm_parse_failures_metric_on_parse_failure():
    """A parse failure must increment LLM_PARSE_FAILURES_TOTAL exactly
    once, tagged with the session_id, so the silent loss becomes
    observable. The metric is owned by the worker (not the parser)
    so a future parser refactor cannot accidentally double-count.
    """
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0))
    metrics = InMemoryMetricsRecorder()

    def clock() -> datetime:
        return datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
    pipeline = Pipeline(
        extraction=ExtractionStage(extractor=MalformedExtractor(), clock=clock),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=HappyEmbedder()),
        indexing=IndexingStage(index=idx),
        store=atoms,
    )
    w = ExtractionWorker(
        events=events, atoms=atoms, pipeline=pipeline,
        metrics=metrics,
    )

    with pytest.raises(LLMParseError):
        w.process_session("s1")

    assert metrics.counter(
        Metrics.LLM_PARSE_FAILURES_TOTAL, tags={"session_id": "s1"}
    ) == 1
    # Parse failure is structurally distinct from an indexing failure.
    # Both counters must NOT be incremented for the same failure —
    # otherwise the dashboard conflates "LLM misbehaved" with
    # "embedder/indexer crashed".
    assert metrics.counter(
        Metrics.INDEXING_FAILURES_TOTAL, tags={"session_id": "s1"}
    ) == 0


def test_worker_parse_failure_is_self_healing_on_retry():
    """After a parse failure, replacing the extractor with a working one
    and re-enqueuing the same session must process every event the
    previous attempt dropped. This is the operator's recovery story:
    fix the prompt / model / wrapper, re-enqueue, no manual cursor
    surgery.
    """
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0))
    events.append(_event(1))
    metrics = InMemoryMetricsRecorder()

    def clock() -> datetime:
        return datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
    pipeline = Pipeline(
        extraction=ExtractionStage(extractor=MalformedExtractor(), clock=clock),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=HappyEmbedder()),
        indexing=IndexingStage(index=idx),
        store=atoms,
    )
    w = ExtractionWorker(
        events=events, atoms=atoms, pipeline=pipeline,
        metrics=metrics,
    )

    with pytest.raises(LLMParseError):
        w.process_session("s1")
    assert atoms.get_cursor("s1") == -1
    assert metrics.counter(
        Metrics.LLM_PARSE_FAILURES_TOTAL, tags={"session_id": "s1"}
    ) == 1

    # Operator swaps the extractor (or fixes the model wrapper).
    pipeline._extraction = ExtractionStage(
        extractor=FixedExtractor(), clock=clock
    )

    indexed = w.process_session("s1")
    assert len(indexed) == 2
    assert atoms.get_cursor("s1") == 1
    # The metric was not double-counted on the successful retry.
    assert metrics.counter(
        Metrics.LLM_PARSE_FAILURES_TOTAL, tags={"session_id": "s1"}
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
