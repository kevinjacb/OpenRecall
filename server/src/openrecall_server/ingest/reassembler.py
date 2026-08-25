"""Per-session audio reassembler.

Orders §C.6 audio packets by ``chunk_seq`` into a single contiguous Opus stream
for the transcriber. The BLE → Android relay → WebSocket path is at-least-once and
can reorder, so this component:

  * delivers frames strictly in ``chunk_seq`` order,
  * buffers out-of-order (future) packets until the gap ahead of them fills,
  * ignores duplicates and already-delivered (old) packets idempotently,
  * advances past VAD gap-markers without emitting audio and without treating
    them as dropouts,
  * exposes the contiguous missing range at the head of the stream so the
    gateway can request a backfill (§E ``request_chunks``).

It deliberately does *not* decide *when* to request backfill (that is a
timeout/policy concern owned by the gateway); it only reports what is missing.
"""

from __future__ import annotations

import logging
from typing import Callable

from .audio_packet import AudioPacket

logger = logging.getLogger(__name__)


class SessionReassembler:
    def __init__(
        self,
        start_seq: int = 0,
        *,
        gap_timeout_ms: int | None = None,
        clock: Callable[[], int | float] | None = None,
    ) -> None:
        self._next = start_seq
        self._buffer: dict[int, AudioPacket] = {}
        # start_seq == 0 means "live stream, no resume point": the device's
        # chunk_seq is a boot-relative monotonic counter that doesn't reset per
        # session, and the phone drops the first few packets before its socket
        # is ready, so the first packet the server sees is at an arbitrary
        # chunk_seq. Anchor the stream there instead of waiting for 0 (which
        # never comes). start_seq > 0 means "resume from here" — the caller
        # named the anchor, so we wait for it and tolerate reordering around it.
        self._anchored = start_seq != 0
        # Bug A: on a continue-stream reconnect the relay resumes at a chunk_seq
        # that leaves a gap (the in-flight chunks were lost while the WebSocket
        # was down; the BLE relay does not buffer past audio, so they will never
        # backfill). Without a deadline the reassembler holds every future
        # packet behind the gap forever and no new audio reaches the
        # transcriber — the 'fails to capture any more audio after reconnect'
        # incident. When ``gap_timeout_ms`` elapses with the head gap still
        # unfilled, re-anchor at the lowest buffered packet and drop the lost
        # range. None (the default) preserves the original wait-forever
        # behaviour for back-compat with tests that don't wire a clock.
        self._gap_timeout_ms = gap_timeout_ms
        self._clock = clock
        self._gap_opened_at: int | float | None = None
        # Frames drained by a wall-clock gap-skip that fired from
        # :meth:`missing_range` (i.e. outside any :meth:`accept` call). The
        # reassembler has no transcriber of its own — decoded frames only
        # reach the transcriber when :meth:`accept` returns them to
        # :meth:`AudioIngestPipeline.ingest`. So a skip triggered by
        # ``missing_range()`` parks the drained frames here; the very next
        # ``accept()`` flushes them into its delivered list, and the pipeline
        # transcribes them as usual. This keeps the skip's wall-clock trigger
        # (independent of ``accept()``) from silently dropping the buffered
        # frames that were ahead of the gap — the 'no frames may be silently
        # dropped' correctness rule.
        self._pending: list[bytes] = []

    @property
    def next_expected_seq(self) -> int:
        return self._next

    @property
    def has_gap(self) -> bool:
        return bool(self._buffer)

    def missing_range(self) -> tuple[int, int] | None:
        """The contiguous gap ``[next_expected, lowest_buffered)`` at the stream head.

        Also opportunistically fires the wall-clock gap-skip so that a gap
        whose deadline elapsed while no ``accept()`` was happening (e.g. the
        server is draining slowly and no new packets have arrived) still
        re-anchors at its wall-clock deadline rather than holding every
        buffered future packet forever. Drained frames are parked in
        ``_pending`` and picked up by the next ``accept()`` — they are NOT
        returned here because the gateway caller of ``missing_range()``
        consumes only the gap tuple, not frames (returning them here would
        silently drop them, since no one transcribes that return value).
        """
        self._maybe_skip_stale_gap()
        if not self._buffer:
            return None
        return (self._next, min(self._buffer))

    def _now(self) -> int | float:
        return self._clock() if self._clock is not None else 0

    def _maybe_skip_stale_gap(self) -> None:
        """If the head gap has been open past the deadline, re-anchor at the
        lowest buffered packet and drain the contiguous head into
        ``_pending`` (so the next :meth:`accept` delivers them to the
        transcriber). A no-op when there is no gap, no deadline is wired, or
        the deadline has not elapsed.

        Called from both :meth:`accept` and :meth:`missing_range` so the
        timer advances on wall-clock (``self._clock``) regardless of whether
        new packets are arriving — the original accept-only call site let a
        server backlog pause the timer past the point where a backfill could
        still arrive usefully."""
        if not self._buffer:
            self._gap_opened_at = None
            return
        if self._gap_timeout_ms is None:
            return
        now = self._now()
        if self._gap_opened_at is None:
            # The gap is (re)opening on this call; start the clock but don't
            # skip yet — a real backfill may be in flight.
            self._gap_opened_at = now
            return
        if now - self._gap_opened_at < self._gap_timeout_ms:
            return
        skip_to = min(self._buffer)
        logger.info(
            "reassembler: head gap [next=%d, %d) unfilled for %dms — "
            "re-anchoring at chunk_seq=%d (dropping %d lost chunk(s))",
            self._next, skip_to, now - self._gap_opened_at,
            skip_to, skip_to - self._next,
        )
        self._next = skip_to
        self._gap_opened_at = None
        # Drain the contiguous head from the new anchor. The frames go into
        # ``_pending``; ``accept()`` flushes ``_pending`` into its delivered
        # list so the pipeline transcribes them. When this skip was triggered
        # from ``missing_range()`` there is no enclosing ``accept()`` yet, so
        # the frames wait here for the next packet to arrive — but they are
        # not lost, and the gap is closed immediately (no further
        # ``request_chunks`` spam for a gap that is already decided unfilled).
        while self._next in self._buffer:
            self._pending.extend(self._buffer.pop(self._next).frames)
            self._next += 1

    def accept(self, packet: AudioPacket) -> list[bytes]:
        """Ingest one packet; return Opus frames now deliverable, in order."""
        seq = packet.chunk_seq

        if not self._anchored:
            # First packet of a fresh live stream (start_seq=0): anchor here.
            # The device's chunk_seq is arbitrary (boot-relative), so we start the
            # stream at the first packet we actually receive.
            self._next = seq
            self._anchored = True
            logger.info("reassembler: anchoring live stream at chunk_seq=%d", seq)

        # Maybe give up on an unfillable head gap (continue-stream reconnect)
        # and re-anchor at the lowest buffered packet. No-op without a deadline.
        # Drained frames (if any) land in _pending.
        self._maybe_skip_stale_gap()
        # Flush any frames drained by a prior missing_range()-triggered skip
        # so the transcriber receives them alongside this packet's frames.
        delivered = self._pending
        self._pending = []

        if seq < self._next:
            logger.debug("reassembler: seq=%d behind next=%d — duplicate/old, dropped", seq, self._next)
            return delivered  # duplicate or already delivered — idempotent drop
        if seq > self._next:
            self._buffer.setdefault(seq, packet)  # future packet; hold for ordering
            # A gap at the head — the gateway will request_chunks to backfill, and
            # no frames are delivered until it fills. Surfaced at INFO so a silent
            # stall is visible at the default level.
            logger.info(
                "reassembler: seq=%d ahead of next=%d — buffered, head gap (held=%d)",
                seq, self._next, len(self._buffer),
            )
            return delivered

        # seq == next_expected: deliver this packet, then drain any contiguous buffer.
        self._gap_opened_at = None  # the gap filled — clear the timer
        delivered.extend(packet.frames)
        self._next += 1
        while self._next in self._buffer:
            delivered.extend(self._buffer.pop(self._next).frames)
            self._next += 1
        logger.debug("reassembler: seq=%d delivered %d frame(s), next=%d", seq, len(delivered), self._next)
        return delivered
