"""The periodic sweep that closes idle segments and titles them (spec §2.1).

Segment close is the one rule in this system that a *clock* decides, not an
event: "no transcript for five minutes" cannot be observed by waiting for the
next transcript, because in the case that matters there isn't one. So it needs
a tick.

That is also what makes it robust. The obvious alternative — close the segment
on ``bye`` — depends on a signal the server usually never receives (spec
§0.3): on a socket drop the relay writes ``bye`` into a dead pipe, and on a
BLE-only drop it sends one for a session it fully intends to keep using. A
timer is indifferent to both.

The sweep also owns titling, because "just closed" is exactly when a segment
is both complete and known to be worth naming.
"""
from __future__ import annotations

import asyncio
import logging

from .segments import SegmentIndex
from .titler import SegmentTitler

log = logging.getLogger(__name__)

# How often to check. Well under SEGMENT_IDLE_MS: the cost is one dictionary
# scan, and the tick interval is added latency on every close.
DEFAULT_INTERVAL_S = 30.0


class SegmentSweeper:
    """Closes idle segments on a tick and titles whatever it closed.

    ``titler`` is optional — without one, segments still close on schedule
    and simply keep their ``preview``. Failure-isolated per segment: one
    unnameable recording must not stop the others closing.
    """

    def __init__(
        self,
        *,
        index: SegmentIndex,
        titler: SegmentTitler | None = None,
        interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        self._index = index
        self._titler = titler
        self._interval = interval_s
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def sweep_once(self) -> int:
        """Close what is idle, title what closed. Returns segments closed.

        Synchronous and directly callable, so the behaviour can be tested
        without a running loop or a sleeping task.
        """
        closed = self._index.close_idle()
        if self._titler is None:
            return len(closed)
        for segment in closed:
            try:
                self._titler.title_segment(segment)
            except Exception:
                # The titler already swallows its own failures; this is the
                # backstop for anything it doesn't anticipate. A segment
                # without a title still renders.
                log.exception("segment_title_sweep_failed segment=%s", segment.id)
        return len(closed)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="segment-sweeper")
        log.info("segment_sweeper_started interval_s=%s", self._interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return  # stop() was called
            except asyncio.TimeoutError:
                pass
            try:
                # The titler makes a blocking LLM call, so the whole sweep
                # goes to a worker thread rather than stalling the event loop
                # (spec ground rule 6).
                await asyncio.to_thread(self.sweep_once)
            except Exception:
                # Never let one bad tick kill the loop — a dead sweeper means
                # segments never close again, silently.
                log.exception("segment_sweep_failed")
