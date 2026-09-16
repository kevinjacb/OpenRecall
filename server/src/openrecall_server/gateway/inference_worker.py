"""Inference worker: decouples ASR inference from the WS receive loop.

One daemon ``threading.Thread`` fed by a bounded ``queue.Queue`` (thread-safe).
The WS receive loop calls :meth:`InferenceWorker.enqueue` (non-blocking) and
keeps pulling packets; inference runs on the worker thread. Results (replies
or ``GatewayError``) are posted back to the event loop via
``loop.call_soon_threadsafe`` so a reply-sender task can write them to the
WebSocket.

Overflow policy: when the queue is full, drop the OLDEST unprocessed inbound
frame and log a warning. This is a last resort, not a lossless operation —
each queued binary frame is a whole §C.6 audio packet that has not reached
the reassembler, so a drop opens a chunk_seq hole (backfill round-trip, or
audio loss on a gap-timeout re-anchor). The default queue depth (32) is
sized so this only fires when the ASR is persistently slower than real time.

Error contract (preserved exactly from the synchronous ``to_thread`` path):

* ``GatewayError`` → posted to the loop so the reply-sender re-raises it,
  closing the WebSocket with code 1002.
* Any other exception → logged and skipped; the link stays up.

The *logging* of that second case is rate-collapsed (see
``_FAILURE_LOG_EVERY``): a full traceback per failed frame is fine when
the backend is in-process and fails once, and a disk-filling bug when ASR is a
network service that is down — one frame per hop, forever, on a box that
records all day. The skip-the-frame behaviour itself is unchanged.

Never uses ``asyncio.Queue`` across threads (the active-plan-2026-07-22
lesson: ``asyncio.Queue.put_nowait`` / ``get_nowait`` are not thread-safe).
The event-loop→worker handoff is ``queue.Queue`` (thread-safe); the
worker→event-loop handoff is ``call_soon_threadsafe`` + a loop-side
``asyncio.Queue`` drained by the reply-sender (single-thread — safe).
"""
from __future__ import annotations

import asyncio
import logging
import queue as _queue
import threading
from collections.abc import Callable
from typing import Any

from .core import GatewayError

logger = logging.getLogger(__name__)

# Type alias for the handle function (handle_message in production).
HandleFn = Callable[[Any, "str | bytes"], list[str]]

#: While the worker is degraded, emit one summary line every N failures (the
#: first failure of an episode always gets a full traceback). Counted, not
#: timed: the hop cadence is the clock that matters here, and a wall-clock
#: rate limiter is neither testable without a flaky timing assertion nor
#: meaningful when the frame rate itself is what varies.
_FAILURE_LOG_EVERY = 100

#: Successful frames required to declare the backend recovered.
#:
#: NOT 1, and this is the whole subtlety: the worker sees *frames*, but only
#: some frames reach the backend. An audio packet is ~200 ms while the Whisper
#: hop is 1 s, so during a total outage roughly four frames in five succeed
#: (they only buffer PCM) and one fails. Resetting on the first success would
#: therefore declare recovery mid-outage and hand the next failure a fresh
#: traceback — a traceback per failed hop, which is exactly the disk-filling
#: behaviour this collapse exists to stop. Observed for real on 2026-09-17:
#: a gateway pointed at a dead inference service logged "recovered after 5
#: consecutive failed frames" while the service was still down.
#:
#: The floor is therefore "comfortably more frames than the hop spans": 50
#: frames is ~10 s at that cadence, long enough that the backend is genuinely
#: answering again rather than momentarily skipped.
_RECOVERY_CLEAN_FRAMES = 50


class InferenceWorker:
    """One inference thread fed by a bounded ``queue.Queue``.

    The WS receive loop calls :meth:`enqueue` (non-blocking, thread-safe —
    safe to call from the event loop thread). The worker thread calls
    ``handle_fn(core, message)`` and posts the result back to the event loop
    via ``loop.call_soon_threadsafe(sink, item)``, where ``item`` is either a
    ``list[str]`` of outbound replies or a ``GatewayError`` to re-raise.

    ``sink`` is called on the event loop thread (via ``call_soon_threadsafe``),
    so it is safe for it to put onto an ``asyncio.Queue`` — that is the
    intended wiring.
    """

    def __init__(
        self,
        handle_fn: HandleFn,
        *,
        core: Any,
        loop: Any,  # asyncio.AbstractEventLoop
        sink: Callable[[Any], None],
        max_queue: int = 32,
    ) -> None:
        self._handle = handle_fn
        self._core = core
        self._loop = loop
        self._sink = sink
        self._q: "_queue.Queue[str | bytes]" = _queue.Queue(maxsize=max_queue)
        # Failure-collapse state. Read and written only on the worker thread,
        # so no lock. ``_degraded`` is the episode flag: it opens on the first
        # failure (the one traceback) and closes only after a clean run long
        # enough to mean something (_RECOVERY_CLEAN_FRAMES), which is what
        # lets a *later* outage log its own traceback without every skipped
        # frame in the current one reopening the episode.
        self._degraded = False
        self._episode_failures = 0
        self._clean_run = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="asr-worker", daemon=True,
        )

    def start(self) -> None:
        """Start the worker thread."""
        self._thread.start()

    def enqueue(self, message: "str | bytes") -> None:
        """Put a message on the queue; on full, drop the oldest message.

        Called from the event loop thread. ``queue.Queue.put_nowait`` is
        thread-safe, so this is safe even though the worker thread may be
        pulling from the same queue concurrently.

        Dropping is a LAST RESORT and it is NOT lossless: each queued message
        is an undecoded inbound frame — for binary frames, a whole §C.6 audio
        packet (up to 200 ms of Opus) that has not reached the reassembler
        yet. Dropping one opens a real chunk_seq hole, which costs a
        request_chunks backfill round-trip at best and, on a gap timeout, a
        re-anchor that loses the audio for good. The queue is sized (default
        32 ≈ 6.4 s of packets) so overflow only happens when the ASR is
        persistently slower than real time; if this warning appears in steady
        state, fix the load (bigger hop, faster backend), don't shrug it off.
        Oldest-first keeps the freshest audio, which minimizes how far the
        transcript falls behind while the hole is repaired.
        """
        try:
            self._q.put_nowait(message)
        except _queue.Full:
            try:
                dropped = self._q.get_nowait()
                _len = len(dropped) if hasattr(dropped, "__len__") else -1
                logger.warning(
                    "asr queue full; dropped oldest inbound frame (len=%d) — "
                    "this is a real audio packet (chunk_seq hole -> backfill/"
                    "re-anchor); ASR is not keeping up with real time", _len,
                )
            except _queue.Empty:
                pass  # raced — queue emptied between put and get
            self._q.put_nowait(message)

    def _run(self) -> None:
        """Worker thread main loop."""
        while not self._stop.is_set():
            try:
                msg = self._q.get(timeout=0.05)
            except _queue.Empty:
                continue
            if self._stop.is_set():
                break
            try:
                replies = self._handle(self._core, msg)
            except GatewayError as exc:
                # Protocol violation — post back to the loop so the
                # reply-sender re-raises it (closes 1002).
                self._loop.call_soon_threadsafe(self._sink, exc)
                continue
            except Exception:
                self._log_failure()
                continue
            self._note_success()
            self._loop.call_soon_threadsafe(self._sink, replies)

    def _log_failure(self) -> None:
        """Log one failed frame without filling the disk during an outage.

        First failure of an episode: the full traceback, at ERROR — the
        diagnostic an operator actually needs. After that, one line every
        ``_FAILURE_LOG_EVERY`` failures carrying the running count, so a
        persistent outage stays visible at a bounded cost. The count is what
        turns a summary line into information: "1200 failures" says roughly
        how long the backend has been gone.
        """
        self._clean_run = 0
        self._episode_failures += 1
        if not self._degraded:
            self._degraded = True
            logger.exception(
                "asr worker: handle_message failed; skipping frame "
                "(further failures collapse to a count every %d)",
                _FAILURE_LOG_EVERY,
            )
        elif self._episode_failures % _FAILURE_LOG_EVERY == 0:
            logger.error(
                "asr worker: still failing — %d frames skipped since the "
                "traceback above", self._episode_failures,
            )

    def _note_success(self) -> None:
        """One line when the backend is really back, then back to silence.

        "Really" is the point: a single success proves nothing, because most
        frames never reach the backend (see ``_RECOVERY_CLEAN_FRAMES``).
        """
        if not self._degraded:
            return
        self._clean_run += 1
        if self._clean_run < _RECOVERY_CLEAN_FRAMES:
            return
        logger.warning(
            "asr worker: recovered after %d failed frames "
            "(their transcripts are lost; the audio is not)",
            self._episode_failures,
        )
        self._degraded = False
        self._episode_failures = 0
        self._clean_run = 0

    async def stop(self) -> None:
        """Signal the worker to stop and join briefly (daemon — won't block).

        The join is offloaded to a thread so the event loop stays responsive
        during the up-to-2s wait for a slow ``handle_message`` to return.
        """
        self._stop.set()
        await asyncio.to_thread(self._thread.join, timeout=2.0)