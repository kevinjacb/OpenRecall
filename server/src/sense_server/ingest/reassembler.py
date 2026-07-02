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

from .audio_packet import AudioPacket


class SessionReassembler:
    def __init__(self, start_seq: int = 0) -> None:
        self._next = start_seq
        self._buffer: dict[int, AudioPacket] = {}

    @property
    def next_expected_seq(self) -> int:
        return self._next

    @property
    def has_gap(self) -> bool:
        return bool(self._buffer)

    def missing_range(self) -> tuple[int, int] | None:
        """The contiguous gap ``[next_expected, lowest_buffered)`` at the stream head."""
        if not self._buffer:
            return None
        return (self._next, min(self._buffer))

    def accept(self, packet: AudioPacket) -> list[bytes]:
        """Ingest one packet; return Opus frames now deliverable, in order."""
        seq = packet.chunk_seq

        if seq < self._next:
            return []  # duplicate or already delivered — idempotent drop
        if seq > self._next:
            self._buffer.setdefault(seq, packet)  # future packet; hold for ordering
            return []

        # seq == next_expected: deliver this packet, then drain any contiguous buffer.
        delivered: list[bytes] = list(packet.frames)
        self._next += 1
        while self._next in self._buffer:
            delivered.extend(self._buffer.pop(self._next).frames)
            self._next += 1
        return delivered
