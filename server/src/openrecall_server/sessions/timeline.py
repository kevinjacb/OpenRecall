"""Device rel_ts -> session mapping for vision snapshot provenance (P3 §3.3).

The device stamps each snapshot with ``rel_ts_ms`` (boot-relative ms). To place
the snapshot on a session's wall-clock timeline the server needs, per session,
the min/max rel_ts it has observed — then ``occurred_at = session.started_at +
(rel_ts_ms - rel_ts_min)``. This is a sidecar to :class:`SessionIndex`: it is fed
from the ingest hot path (which already has ``packet.rel_ts_ms`` in hand) via a
``rel_ts_sink`` callback, and read by the ``POST /media/snapshots`` route.

Thread-safe: written by the ingest worker thread, read by the HTTP route on the
aiohttp loop — a single ``threading.Lock`` mirrors :class:`SessionIndex`.

v1 is single-boot, so sessions are keyed by ``session_id`` alone. Real multi-boot
needs ``boot_id`` in the audio packet (firmware, deferred); the upload payload
already accepts ``boot_id`` for forward-compat.
"""
from __future__ import annotations

import threading


class SessionTimelineIndex:
    def __init__(self) -> None:
        self._ranges: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def record(self, session_id: str, rel_ts_ms: int) -> None:
        with self._lock:
            lo, hi = self._ranges.get(session_id, (rel_ts_ms, rel_ts_ms))
            self._ranges[session_id] = (min(lo, rel_ts_ms), max(hi, rel_ts_ms))

    def rel_range(self, session_id: str) -> tuple[int, int] | None:
        with self._lock:
            return self._ranges.get(session_id)

    def session_for_rel_ts(self, rel_ts_ms: int) -> str | None:
        """Return the session whose [min,max] contains rel_ts_ms.

        On overlap (not expected single-boot), the session with the latest
        ``rel_ts_min`` among the containing ranges wins — deterministic.
        """
        with self._lock:
            best: str | None = None
            best_min = -1
            for sid, (lo, hi) in self._ranges.items():
                if lo <= rel_ts_ms <= hi and lo > best_min:
                    best, best_min = sid, lo
            return best