"""Event-driven extraction worker with reconciliation backstop (M4.3).

The worker is the seam between the audio hot path (which appends to
:class:`~openrecall_server.events.store.EventStore` in real time) and the
memory pipeline (which is slow because it embeds + indexes). The hot
path enqueues a session id; the worker drains the queue and runs the
:class:`~openrecall_server.memory.stages.Pipeline` for that session.

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
        # Each item is (session_id, finalize). ``finalize=False`` is the live
        # path (hold the trailing growing window back); ``finalize=True`` is
        # the session-end path (extract the trailing window too) driven by
        # bye / connection close so an ended session forms memories without a
        # server restart (Bug C).
        self._queue: asyncio.Queue[tuple[str, bool]] = asyncio.Queue(maxsize=capacity)
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
        """Enqueue a session for a *live* extraction pass (``finalize=False``):
        the trailing still-growing 60s window is held back. Called by the
        gateway hot path after every successful transcript append.

        Thread safety:

        - When a loop is bound (the production path), the actual
          ``asyncio.Queue.put_nowait`` runs on the loop thread via
          ``loop.call_soon_threadsafe``. Safe to call from any thread.
        - When no loop is bound (the unit-test fallback), this
          calls ``put_nowait`` directly. Safe only when called from
          the loop thread. The basic enqueue/overflow tests use
          this path; they never call enqueue from a worker thread.
        """
        self._enqueue(session_id, finalize=False)

    def enqueue_finalize(self, session_id: str) -> None:
        """Enqueue a ``finalize=True`` extraction pass: extract the trailing
        partial window too, so an ended session forms memories without a
        server restart (Bug C). Called by the gateway on bye and on
        connection close. Same cross-thread safety as :meth:`enqueue`.
        """
        self._enqueue(session_id, finalize=True)

    def _enqueue(self, session_id: str, *, finalize: bool) -> None:
        if self._loop is not None:
            # Cross-thread producer path. The loop will run the
            # actual put_nowait on its own thread, so the asyncio
            # queue internals are never touched by the calling
            # thread. Overflow is detected on the loop thread; we
            # count it there too.
            self._loop.call_soon_threadsafe(
                self._enqueue_on_loop, session_id, finalize,
            )
            return
        # Loop-thread-only fallback.
        self._enqueue_on_loop(session_id, finalize)

    def _enqueue_on_loop(self, session_id: str, finalize: bool) -> None:
        """The actual put_nowait. Always runs on the loop thread.

        Called either directly (no-loop fallback) or via
        ``loop.call_soon_threadsafe`` (production path). Overflow
        is counted here, on the loop thread, so the metric is
        consistent with the queue state.
        """
        try:
            self._queue.put_nowait((session_id, finalize))
            log.debug(
                "extraction_enqueue session=%s finalize=%s qsize=%d",
                session_id, finalize, self._queue.qsize(),
            )
        except asyncio.QueueFull:
            if self._metrics is not None:
                self._metrics.increment(Metrics.EXTRACTION_QUEUE_OVERFLOW_TOTAL)
            log.warning(
                "extraction_enqueue_overflow session=%s finalize=%s",
                session_id, finalize,
            )

    def enqueue_session(self, session_id: str) -> None:
        """Compatibility shim — same as :meth:`enqueue`."""
        self.enqueue(session_id)

    def qsize(self) -> int:
        return self._queue.qsize()

    async def get(self) -> tuple[str, bool]:
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
        max_parse_failures: int = 5,
        extractor_version: str | None = None,
    ) -> None:
        self._events = events
        self._atoms = atoms
        self._pipeline = pipeline
        self._metrics = metrics
        self._enqueuer = enqueuer or ExtractionEnqueuer(metrics=metrics)
        self._enqueuer.attach(self)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # Version of the extraction algorithm+prompt this worker runs. Stamps
        # the per-session cursor so a version bump invalidates old cursors and
        # the worker re-extracts historical events instead of skipping them.
        # Defaults to the pipeline's own version (ExtractionStage.version).
        self._extractor_version = (
            extractor_version if extractor_version is not None
            else getattr(pipeline, "version", None)
        )
        # P3: listeners invoked after every successful extraction.
        # Snapshot-iterated in process_session so a listener that
        # calls remove_listener during dispatch doesn't mutate the
        # iteration (no skip, no RuntimeError).
        self._listeners: list[Callable[[SessionCompletion], Awaitable[None]]] = list(listeners or [])
        # In-memory consecutive-parse-failure counter for dead-lettering.
        # Resets on a successful extraction; lives only in memory (a
        # restart re-collects up to max_parse_failures failures before
        # dead-lettering again). Kept out of the schema to avoid a
        # migration for this bounded, transient state.
        self._parse_failures: dict[str, int] = {}
        self._max_parse_failures = max_parse_failures

    # --- public API -------------------------------------------------------

    def replace_enqueuer(self, enqueuer: ExtractionEnqueuer) -> None:
        """Swap the worker's enqueuer. Both must be bound to ``self``."""
        self._enqueuer = enqueuer
        enqueuer.attach(self)

    def add_listener(
        self, listener: Callable[[SessionCompletion], Awaitable[None]]
    ) -> None:
        """Register a listener to be called after a successful extraction
        that produced new atoms.

        Listeners are awaited sequentially in registration order.
        Exceptions are caught, logged, and counted as
        ``EXTRACTION_LISTENER_FAILURE_TOTAL`` — they never block the
        worker or other listeners. A batch that extracts no new memories
        (the LLM returned ``[]``) does NOT dispatch — there is nothing
        new to react to, and dispatching on every empty batch would
        flood listeners (and contend on the shared LLM model).
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

        Fire-and-forget on the main event loop (if bound) so a slow
        listener does not block the worker thread that called
        :meth:`process_session`. Each listener is a coroutine;
        exceptions are caught, logged, and counted — never re-raised.
        Snapshot iteration so a listener that calls
        :meth:`remove_listener` during dispatch is safe.

        The worker thread that runs :meth:`process_session` from
        ``_run`` via ``asyncio.to_thread`` has no running event loop
        of its own. Scheduling the listener there with
        ``asyncio.run`` blocks the worker thread for the full
        listener duration (e.g. 2s planner timeout), serialising the
        worker's main drain loop. The proactive listener is also
        written to run on the main event loop (its ``asyncio.Event``,
        ``asyncio.to_thread``-based LLM call, and the drain task all
        live there). So we route to the **bound loop** —
        ``self._enqueuer.bound_loop`` — the main loop bound by
        :meth:`start` via ``enqueuer.bind_loop``.

        Dispatch precedence:

        1. **Running loop** — if :meth:`process_session` was called
           on a thread that already has an event loop running
           (e.g. in-process direct calls from a test), dispatch
           directly on it. Original M4 behavior.
        2. **Bound loop** — if no running loop but the enqueuer
           has a bound loop (the main event loop bound by
           :meth:`start`), use ``call_soon_threadsafe`` to schedule
           on it. This is the production queue-loop path
           (``_run`` → ``to_thread(_safe_process)`` →
           ``process_session`` → ``_dispatch_listeners``). Routing
           to the bound loop runs the listener on the main loop,
           where the planner's ``asyncio.to_thread`` executor, the
           ws_sender's ``asyncio.Event``, and the drain task all
           live. The worker thread is unblocked immediately.
        3. **No loop** — the reconcile sweep runs before
           :meth:`start` binds the loop, on a thread that has no
           loop. Drive the listener on a transient loop in this
           thread. (The original no-loop fallback
           ``listener(completion).close()`` created the coroutine
           and immediately discarded it, leaking a
           ``RuntimeWarning: coroutine '...' was never awaited``.)
        """
        # Detect a running loop ONCE, before creating any coroutine.
        # We must not call `listener(completion)` speculatively and
        # then discard the coroutine if there is no loop — that
        # leaks a `RuntimeWarning: coroutine ... was never awaited`
        # from Python's GC.
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        for listener in list(self._listeners):
            if running_loop is not None:
                # In-process dispatch: running loop is available.
                # Fire-and-forget; exceptions caught by done-callback.
                task = running_loop.create_task(listener(completion))

                def _safe(
                    t: asyncio.Task, sid: str = completion.session_id,
                ) -> None:
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
            elif self._enqueuer.bound_loop is not None:
                # Off-thread dispatch (production queue-loop path).
                # `call_soon_threadsafe` is thread-safe and returns
                # immediately — the worker thread is unblocked.
                try:
                    self._enqueuer.bound_loop.call_soon_threadsafe(
                        self._schedule_listener, listener, completion,
                    )
                except RuntimeError:
                    # Loop is closed (server shutdown). Drop with a
                    # counter — we cannot run the listener on a
                    # closed loop.
                    self._metrics.increment(
                        Metrics.EXTRACTION_LISTENER_FAILURE_TOTAL,
                        tags={
                            "session_id": completion.session_id,
                            "reason": "loop_closed",
                        },
                    )
                    log.exception(
                        "extraction_listener_failed",
                        extra={
                            "session_id": completion.session_id,
                            "reason": "loop_closed",
                        },
                    )
            else:
                # No running loop, no bound loop — reconcile sweep.
                # Drive the listener on a transient loop in this
                # thread. Best-effort: a listener failure is
                # counted and logged.
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

    def _schedule_listener(
        self,
        listener: Callable[[SessionCompletion], Awaitable[None]],
        completion: SessionCompletion,
    ) -> None:
        """Runs on the main event loop. Schedules the listener as a
        task and attaches a done-callback for exception counting.
        Called via ``call_soon_threadsafe`` from the worker thread
        so the worker thread is unblocked immediately.
        """
        task = asyncio.create_task(listener(completion))

        def _safe(
            t: asyncio.Task, sid: str = completion.session_id,
        ) -> None:
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

    def process_session(self, session_id: str, *, finalize: bool = True) -> list:
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

        ``finalize`` is forwarded to :meth:`Pipeline.run`. The live queue
        path passes ``finalize=False`` so the trailing still-growing window
        is held back — the cursor then advances only to ``consumed_seq``
        (the last closed window's last event), NOT to ``last_pending_seq``.
        Reconcile-on-start and the batch scripts use the default
        ``finalize=True`` so a short, ended session still extracts its
        trailing window. Holding the trailing window back is what keeps the
        60s windowing meaningful on the live path: without it the cursor
        advanced past every ~1s fragment and the LLM was called on fragments
        (burning tokens, returning [], never forming memories).

        A *persistent* parse failure is dead-lettered: after
        ``max_parse_failures`` consecutive :class:`LLMParseError` on the
        same session, the cursor advances past the stuck batch so the
        worker stops re-running it on every new event (which would
        re-attempt the whole growing backlog each time). The events
        remain in the event store for re-ingestion after a prompt/model
        fix. Counted as :data:`Metrics.EXTRACTION_DEAD_LETTER_TOTAL` and
        logged as ``extraction_dead_lettered``.
        """
        start = time.monotonic()
        # 1. read events past the cursor
        cursor = self._atoms.get_cursor(session_id, extractor_version=self._extractor_version)
        all_events = self._events.events(session_id)
        # Only transcripts carry extractable speech; a "moment" marker (button
        # press) must not leak its label text into the LLM window. A trailing
        # non-transcript event simply stays past the cursor — re-filtered on
        # the next enqueue, which is cheap and safe.
        pending = [
            e for e in all_events if e.seq > cursor and e.kind == "transcript"
        ]
        if not pending:
            log.debug(
                "extraction_skip session=%s cursor=%d (no pending events)",
                session_id, cursor,
            )
            self._metrics.observe(
                Metrics.EXTRACTION_LATENCY_MS,
                (time.monotonic() - start) * 1000.0,
            )
            return []
        last_pending_seq = max(e.seq for e in pending)
        log.info(
            "extraction_process_start session=%s cursor=%d pending=%d "
            "last_pending_seq=%d finalize=%s",
            session_id, cursor, len(pending), last_pending_seq, finalize,
        )
        # The safe cursor: how far the pipeline can advance. In finalize
        # mode this is last_pending_seq (the trailing window is extracted
        # too). In live mode it is consumed_seq — the last CLOSED window's
        # last event — and the trailing still-growing window is held back
        # (its events remain pending for the next call). Computed here,
        # before the raising pipeline call, so the dead-letter path can
        # advance past the stuck closed window without re-running the LLM.
        if finalize:
            advance_to: int | None = last_pending_seq
        else:
            advance_to = self._pipeline.consumed_seq(pending, finalize=False)
        # 2. run the pipeline (extract -> version -> embed -> index).
        #    The pipeline may raise; the cursor must NOT advance — unless
        #    this is the Nth consecutive parse failure (dead-letter, below).
        try:
            indexed = self._pipeline.run(session_id, pending, finalize=finalize)
            log.info(
                "extraction_process_ok session=%s indexed=%d advance_to=%s "
                "finalize=%s",
                session_id, len(indexed), advance_to, finalize,
            )
        except LLMParseError as exc:
            # Parse failure is structurally distinct from an indexing
            # failure. Count it on its own metric (tagged with the
            # session id) and re-raise so the next enqueue / reconciliation
            # retries the same events. The cursor was not advanced.
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
            self._parse_failures[session_id] = (
                self._parse_failures.get(session_id, 0) + 1
            )
            if self._parse_failures[session_id] >= self._max_parse_failures:
                # Dead-letter: advance past the stuck batch so the worker
                # stops re-running it on every new enqueue. The events
                # remain in the event store for re-ingestion after a
                # prompt/model fix; this only bounds the retry storm.
                # In live mode advance_to holds the trailing growing window
                # back (it was not extracted); only the closed windows the
                # LLM actually ran on are skipped.
                if advance_to is not None:
                    self._atoms.set_cursor(
                        session_id, advance_to,
                        extractor_version=self._extractor_version,
                    )
                self._parse_failures.pop(session_id, None)
                self._metrics.increment(
                    Metrics.EXTRACTION_DEAD_LETTER_TOTAL,
                    tags={"session_id": session_id},
                )
                log.warning(
                    "extraction_dead_lettered",
                    extra={
                        "session_id": session_id,
                        "advanced_to": advance_to,
                        "batch_size": len(pending),
                        "attempts": self._max_parse_failures,
                        "error": str(exc),
                    },
                )
                return []
            log.warning(
                "extraction_parse_failed",
                extra={"session_id": session_id, "error": str(exc)},
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
        # 3. advance the cursor to the last event seq we just processed.
        #    H7: only after extraction AND indexing have both succeeded.
        #    In live mode a None advance_to means no closed window was
        #    extracted (only a growing trailing window) — leave the cursor
        #    where it is so the events are re-seen and the window can grow.
        if advance_to is not None:
            self._atoms.set_cursor(
                session_id, advance_to,
                extractor_version=self._extractor_version,
            )
            log.info(
                "extraction_cursor_advance session=%s cursor=%d->%d",
                session_id, cursor, advance_to,
            )
        else:
            log.info(
                "extraction_cursor_held session=%s cursor=%d (no closed window; "
                "trailing growing window held back)",
                session_id, cursor,
            )
        # A successful extraction resets the consecutive-failure counter.
        self._parse_failures.pop(session_id, None)
        # 4. P3: dispatch to listeners ONLY when new atoms were indexed.
        #    A batch that extracted no memories (the LLM returned [] for
        #    noise / fragments) has nothing to be proactive about —
        #    dispatching on every empty batch floods the planner with LLM
        #    calls, which contend with the extraction worker on the shared
        #    model and push planner calls past their timeout
        #    (proactive_plan_timeout). H7: only after success. Listener
        #    failures are caught, logged, and counted as
        #    EXTRACTION_LISTENER_FAILURE_TOTAL.
        if indexed:
            completion = SessionCompletion(
                session_id=session_id,
                completed_at=datetime.now(tz=timezone.utc),
                event_id_range=(pending[0].seq, pending[-1].seq),
            )
            log.info(
                "extraction_dispatch_listeners session=%s atoms=%d "
                "listeners=%d seq_range=[%d,%d]",
                session_id, len(indexed), len(self._listeners),
                pending[0].seq, pending[-1].seq,
            )
            self._dispatch_listeners(completion)
        else:
            log.debug(
                "extraction_no_dispatch session=%s (no new atoms indexed)",
                session_id,
            )
        self._metrics.observe(
            Metrics.EXTRACTION_LATENCY_MS,
            (time.monotonic() - start) * 1000.0,
        )
        return indexed

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
        log.info("extraction_reconcile_start sessions=%d", len(sessions))
        indexed: list = []
        for sid in sorted(sessions):
            try:
                indexed.extend(self.process_session(sid))
            except LLMParseError:
                # Already counted as LLM_PARSE_FAILURES_TOTAL in
                # process_session. Don't double-count as an indexing
                # failure; just skip this session and keep going.
                continue
            except Exception:
                self._metrics.increment(
                    Metrics.INDEXING_FAILURES_TOTAL,
                    tags={"session_id": sid},
                )
                # Swallow: failure isolation — keep going.
                continue
        log.info("extraction_reconcile_done sessions=%d atoms=%d", len(sessions), len(indexed))
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
        log.info("extraction_worker_started")

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
                session_id, finalize = get_task.result()
                self._enqueuer.task_done()
                log.debug(
                    "extraction_drain session=%s finalize=%s", session_id, finalize,
                )
                # Process on a worker thread so the embedder / indexer
                # don't block the event loop (H4 spirit). The live path
                # enqueues finalize=False after every transcript event to
                # hold the trailing still-growing 60s window back (so the
                # cursor advances only past closed windows and the LLM
                # doesn't burn tokens on ~1s fragments). The session-end
                # path (bye / connection close) enqueues finalize=True to
                # extract the trailing partial window too — Bug C: an ended
                # session forms memories without a server restart.
                await asyncio.to_thread(self._safe_process, session_id, finalize)
            except Exception:
                # Defensive: never let an unhandled exception kill the loop.
                continue

    def _safe_process(self, session_id: str, finalize: bool = False) -> None:
        """Wrap :meth:`process_session` in latency + error metrics."""
        start = time.monotonic()
        try:
            # ``finalize`` is chosen by the enqueue side: the live queue
            # path enqueues False (hold the trailing growing window back);
            # the session-end path enqueues True (extract the trailing
            # partial window too). Reconcile-on-start calls
            # ``reconcile`` -> ``process_session`` directly with
            # finalize=True, bypassing this wrapper.
            self.process_session(session_id, finalize=finalize)
            self._metrics.observe(
                Metrics.EXTRACTION_LATENCY_MS,
                (time.monotonic() - start) * 1000.0,
            )
        except LLMParseError:
            # Already counted as LLM_PARSE_FAILURES_TOTAL (and latency
            # observed) inside process_session. Do NOT also count it as
            # an indexing failure — the two counters must never overlap
            # or the dashboard conflates "LLM misbehaved" with
            # "embedder/indexer crashed".
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
