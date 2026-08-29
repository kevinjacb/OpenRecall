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
                logger.exception(
                    "asr worker: handle_message failed; skipping frame",
                )
                continue
            self._loop.call_soon_threadsafe(self._sink, replies)

    async def stop(self) -> None:
        """Signal the worker to stop and join briefly (daemon — won't block).

        The join is offloaded to a thread so the event loop stays responsive
        during the up-to-2s wait for a slow ``handle_message`` to return.
        """
        self._stop.set()
        await asyncio.to_thread(self._thread.join, timeout=2.0)