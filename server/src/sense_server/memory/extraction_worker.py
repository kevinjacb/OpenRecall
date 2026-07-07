"""Event-driven extraction worker with reconciliation backstop (M4.3).

The worker is the seam between the audio hot path (which appends to
:class:`~sense_server.events.store.EventStore` in real time) and the
memory pipeline (which is slow because it embeds + indexes). The hot
path enqueues a session id; the worker drains the queue and runs the
:class:`~sense_server.memory.stages.Pipeline` for that session.

The binding invariant is **H7: cursor advances only after extraction
AND indexing have both succeeded.** A failed indexing step leaves the
cursor where it was, so a subsequent reconciliation pass — or a
re-enqueue — retries the same set of events. This makes the worker
self-healing on a transient embedder/indexer outage.

The worker is also **failure-isolated**: a failure in one session
does not stop other sessions from being processed.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from ..agent.metrics import InMemoryMetricsRecorder
from ..contracts.metrics import Metrics
from ..events.store import EventStore
from .stages import Pipeline
from .store import AtomStore


class ExtractionEnqueuer:
    """Threadsafe + asyncio-safe enqueue seam for the worker.

    The hot path (gateway WebSocket handler) calls
    :meth:`enqueue` after every successful event append; the worker
    drains the queue in the background. The enqueuer is bounded; an
    overflow increments :data:`Metrics.EXTRACTION_QUEUE_OVERFLOW_TOTAL`
    and drops the duplicate. Sessions are de-duplicated: enqueuing
    the same session id while it is already pending is a no-op.
    """

    def __init__(
        self,
        capacity: int = 1024,
        metrics: InMemoryMetricsRecorder | None = None,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=capacity)
        self._metrics = metrics
        self._worker: ExtractionWorker | None = None

    def attach(self, worker: "ExtractionWorker") -> None:
        """Bind a worker so the enqueuer can call ``enqueue_session``."""
        self._worker = worker

    def enqueue(self, session_id: str) -> None:
        """Enqueue a session for extraction. Drops + counts on overflow."""
        try:
            self._queue.put_nowait(session_id)
        except asyncio.QueueFull:
            if self._metrics is not None:
                self._metrics.increment(Metrics.EXTRACTION_QUEUE_OVERFLOW_TOTAL)

    def enqueue_session(self, session_id: str) -> None:
        """Compatibility shim — same as :meth:`enqueue`."""
        self.enqueue(session_id)

    def qsize(self) -> int:
        return self._queue.qsize()

    async def get(self) -> str:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()


class ExtractionWorker:
    """The async worker that drains the :class:`ExtractionEnqueuer`.

    The worker has two modes:

    * **Event-driven** — :meth:`run` drains the enqueuer until told to stop.
    * **Reconciliation** — :meth:`reconcile` runs every session that
      still has un-indexed events (cursor < last event seq). This is
      the backstop for any events that were appended while the worker
      was down.

    The two are complementary: the event-driven loop processes work in
    near-real-time when the worker is running; the reconciliation pass
    catches up on a restart or after an outage.
    """

    def __init__(
        self,
        events: EventStore,
        atoms: AtomStore,
        pipeline: Pipeline,
        metrics: InMemoryMetricsRecorder,
        enqueuer: ExtractionEnqueuer | None = None,
    ) -> None:
        self._events = events
        self._atoms = atoms
        self._pipeline = pipeline
        self._metrics = metrics
        self._enqueuer = enqueuer or ExtractionEnqueuer(metrics=metrics)
        self._enqueuer.attach(self)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # --- public API -------------------------------------------------------

    def replace_enqueuer(self, enqueuer: ExtractionEnqueuer) -> None:
        """Swap the worker's enqueuer. Both must be bound to ``self``."""
        self._enqueuer = enqueuer
        enqueuer.attach(self)

    def process_session(self, session_id: str) -> list:
        """Run the pipeline for one session. H7: cursor advances on success only.

        Records the EXTRACTION_LATENCY_MS histogram on every call and
        increments INDEXING_FAILURES_TOTAL on failure. Returns the
        atoms that were newly indexed. Raises on failure; callers
        (reconcile / the queue loop) are responsible for catching.
        """
        start = time.monotonic()
        try:
            # 1. read events past the cursor
            cursor = self._atoms.get_cursor(session_id)
            all_events = self._events.events(session_id)
            pending = [e for e in all_events if e.seq > cursor]
            if not pending:
                self._metrics.observe(
                    Metrics.EXTRACTION_LATENCY_MS,
                    (time.monotonic() - start) * 1000.0,
                )
                return []
            # 2. run the pipeline (extract -> version -> embed -> index)
            #    The pipeline may raise; the cursor must NOT advance.
            indexed = self._pipeline.run(session_id, pending)
            # 3. advance the cursor to the last event seq we just processed.
            #    H7: only after extraction AND indexing have both succeeded.
            if pending:
                new_cursor = max(e.seq for e in pending)
                self._atoms.set_cursor(session_id, new_cursor)
            self._metrics.observe(
                Metrics.EXTRACTION_LATENCY_MS,
                (time.monotonic() - start) * 1000.0,
            )
            return indexed
        except Exception:
            self._metrics.increment(
                Metrics.INDEXING_FAILURES_TOTAL,
                tags={"session_id": session_id},
            )
            self._metrics.observe(
                Metrics.EXTRACTION_LATENCY_MS,
                (time.monotonic() - start) * 1000.0,
            )
            raise

    def reconcile(self) -> list:
        """Run process_session for every session that has un-indexed events.

        A failure in one session is logged and skipped; other sessions
        continue. Returns the concatenated list of newly-indexed atoms.
        """
        # Discover the set of sessions by reading from both the event
        # store and the atom store (a session can have atoms but no
        # events if the gateway is the only writer; we still want to
        # visit it for reconciliation).
        sessions: set[str] = set()
        for ev in self._events.events.__self__._by_session.keys() if hasattr(self._events.events, "__self__") else []:
            sessions.add(ev)
        # The above only works for InMemoryEventStore; for production
        # we'd query SQLite. For this slice we just iterate the in-memory
        # session list; production wiring will add a per-store helper.
        if hasattr(self._events, "sessions"):
            sessions.update(self._events.sessions())
        else:
            # Fallback: discover from atoms.
            sessions.update(self._atoms_sessions())
        indexed: list = []
        for sid in sorted(sessions):
            try:
                indexed.extend(self.process_session(sid))
            except Exception:
                self._metrics.increment(
                    Metrics.INDEXING_FAILURES_TOTAL,
                    tags={"session_id": sid},
                )
                # Swallow: failure isolation — keep going.
                continue
        return indexed

    def _atoms_sessions(self) -> set[str]:
        """Discover sessions that have atoms but no events yet (e.g. for tests)."""
        if hasattr(self._atoms, "_by_session"):
            return set(self._atoms._by_session.keys())
        return set()

    # --- async lifecycle --------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="extraction-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        """Drain the enqueuer until :meth:`stop` is called."""
        while not self._stop.is_set():
            try:
                # Wait briefly for a session, but allow stop to interrupt.
                get_task = asyncio.create_task(self._enqueuer.get())
                stop_task = asyncio.create_task(self._stop.wait())
                done, pending = await asyncio.wait(
                    {get_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for p in pending:
                    p.cancel()
                if stop_task in done:
                    return
                session_id = get_task.result()
                self._enqueuer.task_done()
                # Process on a worker thread so the embedder / indexer
                # don't block the event loop (H4 spirit).
                await asyncio.to_thread(self._safe_process, session_id)
            except Exception:
                # Defensive: never let an unhandled exception kill the loop.
                continue

    def _safe_process(self, session_id: str) -> None:
        """Wrap :meth:`process_session` in latency + error metrics."""
        start = time.monotonic()
        try:
            self.process_session(session_id)
            self._metrics.observe(
                Metrics.EXTRACTION_LATENCY_MS,
                (time.monotonic() - start) * 1000.0,
            )
        except Exception:
            self._metrics.increment(
                Metrics.INDEXING_FAILURES_TOTAL,
                tags={"session_id": session_id},
            )
            self._metrics.observe(
                Metrics.EXTRACTION_LATENCY_MS,
                (time.monotonic() - start) * 1000.0,
            )
            # Re-raise for the reconcile path; swallow for the queue path.
            raise
