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

from openrecall_server.agent.metrics import InMemoryMetricsRecorder
from openrecall_server.contracts.metrics import Metrics
from openrecall_server.events.model import CaptureEvent
from openrecall_server.events.store import InMemoryEventStore
from openrecall_server.memory.extract import ExtractedMemory
from openrecall_server.memory.extraction_worker import (
    ExtractionEnqueuer,
    ExtractionWorker,
    SessionCompletion,
)
from openrecall_server.memory.index import InMemoryMemoryIndex
from openrecall_server.memory.stages import (
    EmbeddingStage,
    ExtractionStage,
    IndexingStage,
    Pipeline,
    VersionStampStage,
)
from openrecall_server.memory.store import InMemoryAtomStore


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


def test_listener_runs_when_process_session_called_from_a_thread():
    """Reconcile-on-start calls process_session from a thread
    (``asyncio.to_thread(self._safe_reconcile)`` inside
    ``ExtractionWorker.start``). When there is no event loop, the
    dispatch path used to create the listener coroutine and
    immediately ``.close()`` it — emitting a
    ``RuntimeWarning: coroutine '...' was never awaited`` and
    silently dropping the proactive trigger. The fallback must
    actually run the coroutine.

    Without this test, the in-memory vs production divergence is
    silent: every test that exercises listeners runs inside an
    event loop (``@pytest.mark.asyncio``), so the broken
    fallback path was never observed.
    """
    import threading
    import warnings
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)
    worker = _build_worker(events, atoms, idx, metrics, enq)

    seen: list[str] = []

    async def listener(c: SessionCompletion) -> None:
        seen.append(c.session_id)

    worker.add_listener(listener)
    events.append(_event(0, "s1", "hello"))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        t = threading.Thread(target=worker.process_session, args=("s1",))
        t.start()
        t.join()

    leaked = [w for w in caught if "was never awaited" in str(w.message)]
    assert leaked == [], (
        f"listener coroutine was not awaited when process_session "
        f"ran outside an event loop: {[str(w.message) for w in leaked]}"
    )
    assert seen == ["s1"]


@pytest.mark.asyncio
async def test_listener_does_not_block_worker_run_loop():
    """Regression guard for the production-path bug: the worker thread
    used to block on listener execution because ``process_session`` is
    called from ``_run`` via ``asyncio.to_thread(self._safe_process, ...)``,
    and the worker thread has no running loop. The dispatch then fell
    into a fallback that ran the listener on a transient loop in the
    same thread — blocking the worker for the full listener duration.

    With N sessions and a slow listener, the queue loop must drain in
    O(listener_time) — not O(N * listener_time) — because the listener
    is offloaded to the main event loop, not serialised on the worker
    thread.

    Without this test, the production-path bug is silent: the
    no-loop-fallback test above exercises the same code path as a
    one-shot reconcile call, not the steady-state queue loop. Both
    paths share ``process_session``, so the queue loop's blocking is
    invisible in the existing test suite.
    """
    import time
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=20, metrics=metrics)
    worker = _build_worker(events, atoms, idx, metrics, enq)

    listener_started: list[tuple[str, float]] = []
    listener_done: list[tuple[str, float]] = []
    n_listeners = 3
    listener_seconds = 0.5  # shorter for fast tests; principle is the same
    all_done = asyncio.Event()

    async def slow_listener(c: SessionCompletion) -> None:
        listener_started.append((c.session_id, time.monotonic()))
        await asyncio.sleep(listener_seconds)
        listener_done.append((c.session_id, time.monotonic()))
        if len(listener_done) == n_listeners:
            all_done.set()

    worker.add_listener(slow_listener)

    # Simulate worker.start()'s loop binding + reconcile + start. We
    # only need the loop binding for this test (the reconcile sweep
    # would require a populated event store with prior state).
    enq.bind_loop(asyncio.get_running_loop())
    run_task = asyncio.create_task(worker._run())

    try:
        # Enqueue N sessions quickly. The queue is drained by _run.
        # _safe_process runs the live path (finalize=False), so each session
        # needs a CLOSED window for the LLM to run and produce an atom that
        # fires the listener. _build_worker uses the default window_ms=60s
        # with start_ms=seq*1000, so event 60 (start_ms=60000) closes the
        # window started by event 0; that window is extracted and the
        # trailing [e60] window is held back.
        for i in range(n_listeners):
            events.append(_event(0, f"s{i}", "hello"))
            events.append(_event(60, f"s{i}", "hello"))
            enq.enqueue(f"s{i}")

        # Wait for all listeners to complete. With the fix, this is
        # ~listener_seconds. Without the fix, this is ~N*listener_seconds
        # because the worker thread is serialised behind each listener.
        t0 = time.monotonic()
        await asyncio.wait_for(all_done.wait(), timeout=10.0)
        elapsed = time.monotonic() - t0

        # All N listeners fired.
        assert len(listener_done) == n_listeners
        assert sorted(s for s, _ in listener_done) == [
            f"s{i}" for i in range(n_listeners)
        ]
        # The listeners ran in parallel on the main loop, not serialised
        # on the worker thread. Allow generous slack for CI; the bug
        # is ~N*listener_seconds (1.5s for N=3) vs the fix ~listener_seconds
        # (0.5s). A 1.2s threshold catches the regression and forgives
        # cold-start overhead.
        assert elapsed < 1.2, (
            f"listeners blocked the worker thread: "
            f"{n_listeners} sessions with a {listener_seconds}s listener "
            f"took {elapsed:.2f}s (expected < 1.2s). "
            f"listener_done times: {listener_done}"
        )
    finally:
        # Stop the worker's _run loop and let it drain.
        enq.enqueue("__stop__")  # any session id; _safe_process will run
        # Cancel cleanly.
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass


class EmptyExtractor:
    """Always returns no memories — the LLM said 'nothing memorable' ([])."""
    def extract(self, text: str) -> list[ExtractedMemory]:
        return []


async def test_listener_not_invoked_when_no_new_atoms_extracted():
    """A batch that extracts NO new memories (the LLM returned [] for noise
    / fragments) must NOT fire listeners — there is nothing new to be
    proactive about. Firing on every empty batch floods the planner with LLM
    calls (contention on the shared model → proactive_plan_timeout). The
    cursor still advances (the pipeline succeeded).
    """
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)

    def clock() -> datetime:
        return datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
    pipeline = Pipeline(
        extraction=ExtractionStage(extractor=EmptyExtractor(), clock=clock),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=HappyEmbedder()),
        indexing=IndexingStage(index=idx),
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
    events.append(_event(1, "s1", "world"))
    worker.process_session("s1")
    await _drain_listeners()

    assert seen == [], "listener must not fire when no new atoms were extracted"
    # The cursor still advanced (the pipeline succeeded; nothing to retry).
    assert atoms.get_cursor("s1") == 1
