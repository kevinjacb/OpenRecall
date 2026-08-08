"""Is the device actually still talking to us? (spec §5.1, D9)

The Settings header wants to say something true about device health. Battery
telemetry does not exist at any layer — no ADC channel, no fuel-gauge driver,
no ``vbat`` anywhere in the firmware — so it is parked (D9). But the server
already knows something real and more actionable: whether packets are still
arriving.

**Why packet arrival is a heartbeat and not a speech detector.** Firmware VAD
suppresses silence, but it keeps emitting ``C6_GAP_MARKER`` packets while
suppressing, specifically to keep ``chunk_seq``/``rel_ts`` contiguous. So
packets flow whether or not anyone is talking. A stale ``last_packet_at``
therefore means the device stopped talking to *us* — which is the failure
worth surfacing — rather than "the room went quiet".

Pairing it with the last transcript separates the three states a user
actually cares about: alive and hearing speech, alive in a quiet room, and
gone.

**Ages come from a monotonic clock.** The wall clock is kept only for
display. A system clock step (NTP correction, timezone fix, a laptop waking
from sleep) would otherwise produce a negative or absurd age on a device that
is perfectly healthy.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class LivenessSnapshot:
    """A consistent read of the liveness state.

    Every field is ``None`` until the corresponding event has happened at
    least once — "we have never heard from this device" is a distinct answer
    from "we heard from it a long time ago", and the header should not
    conflate them.
    """

    connected: bool = False
    last_packet_at: datetime | None = None
    last_packet_age_s: float | None = None
    last_transcript_at: datetime | None = None
    last_transcript_age_s: float | None = None


class DeviceLiveness:
    """Mutable liveness state, written by the gateway and read by HTTP.

    Thread-safe for the same reason :class:`SessionIndex` is: the gateway
    writes from a worker thread (``asyncio.to_thread`` in the adapter) while
    HTTP handlers read on the event loop.
    """

    def __init__(self, *, monotonic=time.monotonic, wall=None) -> None:
        self._monotonic = monotonic
        self._wall = wall or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._connections = 0
        self._packet_at: tuple[datetime, float] | None = None
        self._transcript_at: tuple[datetime, float] | None = None

    # -- writers -------------------------------------------------------------

    def connection_opened(self) -> None:
        with self._lock:
            self._connections += 1

    def connection_closed(self) -> None:
        with self._lock:
            # Clamp at zero: a close without a matching open (a duplicated
            # `finally`, a partially-constructed connection) must not drive
            # the count negative and make `connected` permanently false.
            self._connections = max(0, self._connections - 1)

    def packet_received(self) -> None:
        """Stamp an inbound audio packet — including a gap marker."""
        with self._lock:
            self._packet_at = (self._wall(), self._monotonic())

    def transcript_emitted(self) -> None:
        with self._lock:
            self._transcript_at = (self._wall(), self._monotonic())

    # -- reader --------------------------------------------------------------

    def snapshot(self) -> LivenessSnapshot:
        now = self._monotonic()
        with self._lock:
            packet = self._packet_at
            transcript = self._transcript_at
            connected = self._connections > 0
        return LivenessSnapshot(
            connected=connected,
            last_packet_at=packet[0] if packet else None,
            last_packet_age_s=round(now - packet[1], 3) if packet else None,
            last_transcript_at=transcript[0] if transcript else None,
            last_transcript_age_s=round(now - transcript[1], 3) if transcript else None,
        )
