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
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Optional

from ..agent.metrics import InMemoryMetricsRecorder
from ..contracts.metrics import Metrics
from ..events.store import EventStore
from .extract import LLMParseError
from .stages import Pipeline
from .store import AtomStore

log = logging.getLogger(__name__)


class SessionCompletion:
    """The completion record dispatched to listeners after a successful
    extraction. Carries the session id, the wall-clock time of completion
    (injected via a clock so tests are deterministic), and the inclusive
    ``(first, last)`` event sequence range that was just processed.

    P3: listeners registered on :class:`ExtractionWorker` receive one
    of these after every successful ``process_session`` call. The
    primary listener is the :class:`ProactiveTriggerEngine`, which
    turns the completion into a Proactive planner call.
    """

    __slots__ = ("session_id", "completed_at", "event_id_range")

    def __init__(
        self,
        session_id: str,
        completed_at: datetime,
        event_id_range: tuple[int, int],
    ) -> None:
        self.session_id = session_id
        self.completed_at = completed_at
        self.event_id_range = event_id_range

    def __repr__(self) -> str:
        return (
            f"SessionCompletion(session_id={self.session_id!r}, "
            f"completed_at={self.completed_at!r}, "
            f"event_id_range={self.event_id_range!r})"
        )


class ExtractionEnqueuer:
    """Threadsafe + asyncio-safe enqueue seam for the worker.

    The hot path (gateway WebSocket handler) calls
    :meth:`enqueue` after every successful event append; the worker
    drains the queue in the background. The enqueuer is bounded; an
    overflow increments :data:`Metrics.EXTRACTION_QUEUE_OVERFLOW_TOTAL`
    and drops the duplicate. Sessions are de-duplicated: enqueuing
    the same session id while it is already pending is a no-op.

    Cross-thread contract:

    The gateway adapter runs ``handle_message`` on a worker thread
    via ``asyncio.to_thread`` (``gateway/adapter.py``). That worker
    thread is the producer for the enqueuer; the consumer is the
    ``ExtractionWorker._run`` task on the event loop. ``asyncio.Queue``
    is documented as not thread-safe — the queue's internals
    (``_unfinished_tasks``, ``_finished``, ``_getters``) are
    manipulated without any locking, and CPython does not provide
    atomicity for the compound operations. A direct
    ``self._queue.put_nowait`` from the worker thread races the
    loop thread on every put.

    The fix: when a loop is bound (via :meth:`bind_loop`, called
    from :meth:`ExtractionWorker.start`), the producer path uses
    ``loop.call_soon_threadsafe(self._queue.put_nowait, session_id)``
    so the actual queue manipulation happens on the loop thread.
    When no loop is bound (the test-only fallback), the producer
    path uses ``put_nowait`` directly — this is documented as
    loop-thread-only and is safe for the unit tests that
    construct an enqueuer without a worker.
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
        # The loop the consumer (worker._run) runs on. Captured by
        # bind_loop from worker.start(); None in unit tests that
        # exercise the enqueuer directly. The cross-thread producer
        # path uses call_soon_threadsafe when this is set.
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def bound_loop(self) -> asyncio.AbstractEventLoop | None:
        """The loop the enqueuer is bound to, or None if unbound.

        Set by :meth:`bind_loop` (called from
        :meth:`ExtractionWorker.start`); consulted by :meth:`enqueue`
        to decide whether to use the cross-thread-safe
        ``call_soon_threadsafe`` path or the loop-thread-only
        direct ``put_nowait`` path. Exposed as a public attribute
        so the structural regression tests can assert the bind
        happened.
        """
        return self._loop

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the loop the consumer (worker._run) runs on.

        Called from :meth:`ExtractionWorker.start`, which itself
        runs on the event loop. After this call, the producer
        path (gateway hot path) can safely call :meth:`enqueue`
        from any thread: the actual ``asyncio.Queue.put_nowait``
        happens on the loop thread via ``call_soon_threadsafe``.

        Re-binding to a different loop is a programming error:
        raises :class:`RuntimeError`. Re-binding to the same
        loop is a no-op.
        """
        if self._loop is None:
            self._loop = loop
            return
        if self._loop is loop:
            return
        raise RuntimeError(
            "ExtractionEnqueuer is already bound to a different loop; "
            "do not share an enqueuer across loops."
        )

    def attach(self, worker: "ExtractionWorker") -> None:
        """Bind a worker so the enqueuer can call ``enqueue_session``."""
        self._worker = worker

    def enqueue(self, session_id: str) -> None:
        """Enqueue a session for extraction. Drops + counts on overflow.

        Thread safety:

        - When a loop is bound (the production path), the actual
          ``asyncio.Queue.put_nowait`` runs on the loop thread via
          ``loop.call_soon_threadsafe``. Safe to call from any thread.
        - When no loop is bound (the unit-test fallback), this
          calls ``put_nowait`` directly. Safe only when called from
          the loop thread. The basic enqueue/overflow tests use
          this path; they never call enqueue from a worker thread.
        """
        if self._loop is not None:
            # Cross-thread producer path. The loop will run the
            # actual put_nowait on its own thread, so the asyncio
            # queue internals are never touched by the calling
            # thread. Overflow is detected on the loop thread; we
            # count it there too.
            self._loop.call_soon_threadsafe(self._enqueue_on_loop, session_id)
            return
        # Loop-thread-only fallback.
        self._enqueue_on_loop(session_id)

    def _enqueue_on_loop(self, session_id: str) -> None:
        """The actual put_nowait. Always runs on the loop thread.

        Called either directly (no-loop fallback) or via
        ``loop.call_soon_threadsafe`` (production path). Overflow
        is counted here, on the loop thread, so the metric is
        consistent with the queue state.
        """
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
        listeners: list[Callable[[SessionCompletion], Awaitable[None]]] | None = None,
    ) -> None:
        self._events = events
        self._atoms = atoms
        self._pipeline = pipeline
        self._metrics = metrics
        self._enqueuer = enqueuer or ExtractionEnqueuer(metrics=metrics)
        self._enqueuer.attach(self)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # P3: listeners invoked after every successful extraction.
        # Snapshot-iterated in process_session so a listener that
        # calls remove_listener during dispatch doesn't mutate the
        # iteration (no skip, no RuntimeError).
        self._listeners: list[Callable[[SessionCompletion], Awaitable[None]]] = list(listeners or [])

    # --- public API -------------------------------------------------------

    def replace_enqueuer(self, enqueuer: ExtractionEnqueuer) -> None:
        """Swap the worker's enqueuer. Both must be bound to ``self``."""
        self._enqueuer = enqueuer
        enqueuer.attach(self)

    def add_listener(
        self, listener: Callable[[SessionCompletion], Awaitable[None]]
    ) -> None:
        """Register a listener to be called after every successful extraction.

        Listeners are awaited sequentially in registration order.
        Exceptions are caught, logged, and counted as
        ``EXTRACTION_LISTENER_FAILURE_TOTAL`` — they never block the
        worker or other listeners.
        """
        self._listeners.append(listener)

    def remove_listener(
        self, listener: Callable[[SessionCompletion], Awaitable[None]]
    ) -> None:
        """Remove a previously-registered listener. No-op if absent."""
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    def _dispatch_listeners(self, completion: SessionCompletion) -> None:
        """Schedule each registered listener to run after the cursor advance.

        Fire-and-forget on the running event loop (if any). Each
        listener is a coroutine; exceptions are caught, logged, and
        counted — never re-raised. Snapshot iteration so a listener
        that calls remove_listener during dispatch is safe.

        No-loop fallback: ``process_session`` is also called from
        :meth:`start`'s reconcile sweep, which runs on a worker
        thread via ``asyncio.to_thread`` — there is no event loop
        on that thread. In that case we run the listener on a
        transient loop in the same thread. (The previous fallback
        ``listener(completion).close()`` created the coroutine and
        immediately discarded it, leaking a
        ``RuntimeWarning: coroutine '...' was never awaited`` and
        silently dropping the proactive trigger. The reconcile
        sweep is the first production caller of this fallback
        path; before M4.3 the queue loop was the only caller and
        always had a loop, so the broken fallback was
        untested-and-unused.)
        """
        # Detect a running loop ONCE, before creating any coroutine.
        # We must not call `listener(completion)` speculatively and
        # then discard the coroutine if there is no loop — that
        # leaks a `RuntimeWarning: coroutine ... was never awaited`
        # from Python's GC. (The original fallback did exactly that,
        # hence the warning. The proactive trigger also silently
        # dropped in production until M4.3 added a test that
        # exercises the no-loop path.)
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        for listener in list(self._listeners):
            if running_loop is not None:
                # Fire-and-forget on the existing loop. Exceptions
                # are caught by the done-callback.
                task = running_loop.create_task(listener(completion))

                def _safe(t: asyncio.Task, sid: str = completion.session_id) -> None:
                    try:
                        t.result()
                    except Exception:
                        self._metrics.increment(
                            Metrics.EXTRACTION_LISTENER_FAILURE_TOTAL,
                            tags={"session_id": sid},
                        )
                        log.exception(
                            "extraction_listener_failed",
                            extra={"session_id": sid},
                        )

                task.add_done_callback(_safe)
            else:
                # No running loop (e.g. process_session called from
                # a worker thread by `asyncio.to_thread` in start()).
                # `asyncio.run` creates a transient loop, drives the
                # coroutine to completion, and tears the loop down.
                try:
                    asyncio.run(listener(completion))
                except Exception:
                    self._metrics.increment(
                        Metrics.EXTRACTION_LISTENER_FAILURE_TOTAL,
                        tags={"session_id": completion.session_id},
                    )
                    log.exception(
                        "extraction_listener_failed",
                        extra={"session_id": completion.session_id},
                    )

    def process_session(self, session_id: str) -> list:
        """Run the pipeline for one session. H7: cursor advances on success only.

        Records the EXTRACTION_LATENCY_MS histogram on every call. On
        failure, the counter depends on the failure type:

        * :class:`LLMParseError` — the LLM reply did not match the
          contracted shape. This is a *parse* failure, not an indexing
          failure, and the metric is
          :data:`Metrics.LLM_PARSE_FAILURES_TOTAL`. The cursor is
          left unchanged so the next enqueue / reconciliation retries
          the same events. Distinct from INDEXING_FAILURES_TOTAL so
          the dashboard does not conflate "LLM misbehaved" with
          "embedder/indexer crashed".
        * Any other exception — embedder / indexer / store failure.
          Increments :data:`Metrics.INDEXING_FAILURES_TOTAL` as before.

        Returns the atoms that were newly indexed. Raises on failure;
        callers (reconcile / the queue loop) are responsible for catching.
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
            # 4. P3: dispatch to listeners after the cursor advances.
            #    H7: only after success. Listener failures are caught,
            #    logged, and counted as EXTRACTION_LISTENER_FAILURE_TOTAL.
            if pending:
                completion = SessionCompletion(
                    session_id=session_id,
                    completed_at=datetime.now(tz=timezone.utc),
                    event_id_range=(pending[0].seq, pending[-1].seq),
                )
                self._dispatch_listeners(completion)
            self._metrics.observe(
                Metrics.EXTRACTION_LATENCY_MS,
                (time.monotonic() - start) * 1000.0,
            )
            return indexed
        except LLMParseError:
            # Parse failure is structurally distinct from an indexing
            # failure. Count it on its own metric (tagged with the
            # session id) and re-raise. The cursor was not advanced.
            # NOTE: do NOT also increment INDEXING_FAILURES_TOTAL here
            # — the two counters must never overlap or the dashboard
            # conflates "LLM misbehaved" with "embedder/indexer crashed".
            self._metrics.increment(
                Metrics.LLM_PARSE_FAILURES_TOTAL,
                tags={"session_id": session_id},
            )
            self._metrics.observe(
                Metrics.EXTRACTION_LATENCY_MS,
                (time.monotonic() - start) * 1000.0,
            )
            log.warning(
                "extraction_parse_failed",
                extra={"session_id": session_id},
            )
            raise
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

        Session discovery goes through :meth:`EventStore.sessions`
        (both backends implement it after the M4.3 reconciliation
        pass landed). The previous implementation tried to introspect
        ``self._events.events.__self__._by_session`` (an
        :class:`InMemoryEventStore` private attribute) and fell
        through to ``_atoms_sessions`` for the sqlite case — both
        fragile and broken in production. Replaced with the
        protocol-level ``sessions()`` method, which is a single
        ``SELECT DISTINCT session_id`` against sqlite and a
        ``list(self._by_session.keys())`` against the in-memory store.
        """
        sessions = list(self._events.sessions())
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

    # --- async lifecycle --------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        # Bind the running loop to the enqueuer so the cross-thread
        # producer path (gateway hot path → enqueue from a worker
        # thread created by `asyncio.to_thread`) is safe. The
        # enqueuer's enqueue() uses loop.call_soon_threadsafe when
        # bound, so the asyncio.Queue internals are only touched
        # on the loop thread. The structural regression test
        # `test_worker_start_binds_the_loop_to_the_enqueuer` pins
        # this binding.
        self._enqueuer.bind_loop(asyncio.get_running_loop())
        # Reconcile-on-start (M4.3 reconciliation). The live enqueuer
        # only sees new events from the gateway; any session that
        # accumulated events while the worker was down (e.g. an
        # `events.db` that already has rows from a previous run, or
        # the first start after a fresh deploy) needs an explicit
        # sweep. We do this on a worker thread so a slow LLM-based
        # extractor doesn't block the event loop. H7 still holds:
        # `process_session` advances the cursor only after the full
        # extract+embed+index pipeline succeeds, so a partially-indexed
        # session is correctly re-attempted on the next start.
        await asyncio.to_thread(self._safe_reconcile)
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

    def _safe_reconcile(self) -> None:
        """Wrap :meth:`reconcile` so a per-session failure doesn't abort
        the whole sweep. The sweep is best-effort: the live enqueuer
        is the primary path; reconcile is the backstop for events that
        landed while the worker was down. A failed sweep on start is
        not fatal — the next start retries, and a single bad session
        doesn't prevent the others from being processed."""
        try:
            self.reconcile()
        except Exception:
            log.exception("extraction_reconcile_on_start_failed")
            self._metrics.increment(Metrics.INDEXING_FAILURES_TOTAL)
