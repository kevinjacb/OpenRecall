"""Segments — the derived "recording" the app actually shows (spec D1, §2.1).

A *session* is not a recording. The Android relay mints one session id in
``onStartCommand`` and reuses it across every BLE/WebSocket reconnect; nothing
ends it — no timer, no idle rollover, no day boundary. A session is "however
long the foreground service lived", which is hours to days. The design shows
discrete recordings ("Studio standup · 01:04"), so the two are different
things and the app needs the smaller one.

A **segment** is a contiguous run of transcript activity inside a session,
cut by two rules:

* **Idle close** — no transcript event for :data:`SEGMENT_IDLE_MS` closes the
  segment at the end of its last event.
* **Hard cap** — a segment spanning :data:`SEGMENT_MAX_MS` force-closes, so a
  genuinely continuous eight-hour day still becomes browsable rows.

Two properties make this safe to build durable metadata on:

* **Ids are deterministic.** ``segment_id = f"{session_id}:{first_seq}"``,
  following the existing ``event_id = f"{session_id}:{seq}"`` precedent, so a
  cold-start rebuild reproduces identical ids and a title written last week
  stays attached to the same segment.
* **Closing needs a clock, not an event.** The idle rule can only fire when
  time passes, so :meth:`SegmentIndex.close_idle` is driven by a periodic
  sweep. That is deliberate: it means segment close does not depend on
  ``bye``, which is unreliable (spec §0.3) — a dropped socket writes it into
  a dead pipe and the server never sees one.

Like :class:`~openrecall_server.sessions.index.SessionIndex`, this index is
derived state rebuilt from the event store on startup. Nothing durable may
live only here.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from ..events.model import CaptureEvent
from ..events.store import EventStore
from ..paging import decode_cursor, encode_cursor

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]


def _as_aware(value: datetime) -> datetime:
    """Coerce a naive datetime to UTC so a cursor comparison never raises."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

# No transcript for this long closes the segment. Five minutes is long enough
# to survive a pause in a real conversation and short enough that a lunch
# break does not glue the morning to the afternoon.
SEGMENT_IDLE_MS = 300_000
# A segment can never exceed this. Without it, a continuously-noisy room
# produces one unbrowsable row per day.
SEGMENT_MAX_MS = 3_600_000

PREVIEW_MAX_CHARS = 80


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _truncate(text: str, limit: int = PREVIEW_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


@dataclass
class Segment:
    """One contiguous run of transcript activity.

    ``started_at``/``ended_at`` are wall clock (what the app groups by);
    ``first_seq``/``last_seq`` are the event range (what audio, memory and
    delete all slice on). Both are needed: the wall clock is not derivable
    from seq, and seq is what every other store is keyed by.
    """

    id: str
    session_id: str
    started_at: datetime
    ended_at: datetime
    first_seq: int
    last_seq: int
    transcript_count: int = 0
    preview: str | None = None
    # Start and end on the session's audio timeline, in ms. This is the
    # window the audio plane (Phase 3) and the range deletes (§5.2) cut on.
    start_ms: int = 0
    end_ms: int = 0
    closed: bool = field(default=False)

    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


class SegmentIndex:
    """Thread-safe in-memory segment index, derived from the event stream.

    Mutators: :meth:`record` (from ``GatewayCore._emit``, alongside
    ``SessionIndex.record``) and :meth:`close_idle` (from the periodic
    sweeper). Readers: :meth:`get`, :meth:`list`, :meth:`for_session`.

    Thread safety mirrors :class:`SessionIndex`: the gateway records from a
    worker thread while HTTP handlers read on the event loop, so one lock
    guards the dict.
    """

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        idle_ms: int = SEGMENT_IDLE_MS,
        max_ms: int = SEGMENT_MAX_MS,
    ) -> None:
        self._segments: dict[str, Segment] = {}
        # session_id -> the id of its still-open segment, if any.
        self._open: dict[str, str] = {}
        self._lock = threading.Lock()
        self._clock: Clock = clock or _default_clock
        self._idle = timedelta(milliseconds=idle_ms)
        self._max_ms = max_ms

    # -- mutators ------------------------------------------------------------

    def record(self, event: CaptureEvent) -> Segment:
        """Fold one stored transcript event into its segment.

        Opens a new segment when the session has none open, when the gap
        since the last event exceeds the idle threshold, or when the open
        segment has hit the hard cap. Returns the segment the event landed
        in, which is what the caller needs to attach audio and titles.
        """
        with self._lock:
            return self._record_locked(event)

    def _record_locked(self, event: CaptureEvent) -> Segment:
        open_id = self._open.get(event.session_id)
        current = self._segments.get(open_id) if open_id else None
        if current is not None and self._should_close(current, event):
            current.closed = True
            self._open.pop(event.session_id, None)
            current = None
        if current is None:
            current = Segment(
                id=f"{event.session_id}:{event.seq}",
                session_id=event.session_id,
                started_at=event.created_at,
                ended_at=event.created_at,
                first_seq=event.seq,
                last_seq=event.seq,
                start_ms=event.start_ms,
                end_ms=event.start_ms,
            )
            self._segments[current.id] = current
            self._open[event.session_id] = current.id
        current.last_seq = max(current.last_seq, event.seq)
        current.ended_at = max(current.ended_at, event.created_at)
        current.end_ms = max(current.end_ms, event.start_ms + event.duration_ms)
        if event.kind == "transcript":
            current.transcript_count += 1
            if current.preview is None:
                stripped = (event.text or "").strip()
                if stripped:
                    current.preview = _truncate(stripped)
        return current

    def _should_close(self, segment: Segment, event: CaptureEvent) -> bool:
        """Whether ``event`` belongs to a *new* segment rather than this one.

        The idle test uses wall clock (that is what "I stopped talking for
        five minutes" means) while the cap uses the audio timeline (that is
        what "this recording is an hour long" means). Using one clock for
        both would make a reconnect gap count against the cap, or a long
        silent stretch count as recorded length.
        """
        if event.created_at - segment.ended_at >= self._idle:
            return True
        return (event.start_ms + event.duration_ms) - segment.start_ms >= self._max_ms

    def close_idle(self, *, now: datetime | None = None) -> list[Segment]:
        """Close every open segment whose last event is older than the idle
        threshold. Returns the segments just closed.

        This is the sweeper's entry point, and it is what makes segment close
        survive a lost ``bye`` (spec §0.3). The returned list is the titling
        work queue — a segment is only worth naming once it is complete.
        """
        cutoff = (now or self._clock()) - self._idle
        closed: list[Segment] = []
        with self._lock:
            for session_id, segment_id in list(self._open.items()):
                segment = self._segments.get(segment_id)
                if segment is None:
                    self._open.pop(session_id, None)
                    continue
                if segment.ended_at <= cutoff:
                    segment.closed = True
                    self._open.pop(session_id, None)
                    closed.append(segment)
        if closed:
            log.info("segments_closed_idle count=%d", len(closed))
        return closed

    def close_session(self, session_id: str) -> Segment | None:
        """Close a session's open segment because the session itself ended.

        Called on ``bye`` — a best-effort accelerator, not the mechanism.
        :meth:`close_idle` closes the same segment moments later anyway,
        which is what keeps this correct when ``bye`` never arrives.
        """
        with self._lock:
            segment_id = self._open.pop(session_id, None)
            if segment_id is None:
                return None
            segment = self._segments.get(segment_id)
            if segment is None:
                return None
            segment.closed = True
            return segment

    def delete(self, segment_id: str) -> bool:
        """Drop a segment from the index (spec §5.2). Idempotent."""
        with self._lock:
            segment = self._segments.pop(segment_id, None)
            if segment is None:
                return False
            if self._open.get(segment.session_id) == segment_id:
                self._open.pop(segment.session_id, None)
            return True

    def rebuild_from_store(self, event_store: EventStore) -> None:
        """Cold-start rebuild by replaying every event through :meth:`record`.

        Deterministic ids are what make this safe: the same events produce
        the same segment ids, so durable metadata keyed by them (titles)
        stays attached across a restart.

        Every replayed segment is left **closed** except the newest one per
        session: historical segments cannot be resumed by definition, and
        leaving them open would make the next sweep re-fire their titling.
        Failure-isolated per session, like ``SessionIndex.rebuild_from_store``
        — one corrupt session must not cost the user every other recording.
        """
        with self._lock:
            self._segments.clear()
            self._open.clear()
        for session_id in sorted(event_store.sessions()):
            try:
                for event in event_store.events(session_id):
                    self.record(event)
            except Exception:
                log.exception(
                    "segment_index_rebuild_session_failed",
                    extra={"session_id": session_id},
                )
                continue
        # A rebuilt open segment is only genuinely open if the device is
        # still streaming into it; the idle sweep decides that on its own
        # schedule, so nothing else is needed here.

    # -- readers -------------------------------------------------------------

    def get(self, segment_id: str) -> Segment | None:
        with self._lock:
            return self._segments.get(segment_id)

    def total(self) -> int:
        with self._lock:
            return len(self._segments)

    def for_session(self, session_id: str) -> list[Segment]:
        with self._lock:
            return sorted(
                (s for s in self._segments.values() if s.session_id == session_id),
                key=lambda s: s.first_seq,
            )

    def segment_for_event(self, session_id: str, seq: int) -> Segment | None:
        """The segment containing ``(session_id, seq)``, or None.

        Used by transcript search (§2.4) to map an event hit back to the row
        the user taps.
        """
        with self._lock:
            for s in self._segments.values():
                if s.session_id == session_id and s.first_seq <= seq <= s.last_seq:
                    return s
        return None

    def all(self) -> list[Segment]:
        with self._lock:
            return list(self._segments.values())

    def list(
        self,
        *,
        limit: int,
        before: str | None = None,
        session_id: str | None = None,
    ) -> tuple[list[Segment], str | None]:
        """A page of segments, newest first by ``started_at``.

        Same opaque keyset cursor as ``/sessions`` (see
        :mod:`openrecall_server.paging`). Raises :class:`ValueError` on a
        malformed cursor so the route can 400 instead of silently restarting
        pagination.
        """
        if limit <= 0:
            raise ValueError(f"limit must be positive, got {limit}")
        with self._lock:
            candidates: Iterable[Segment] = self._segments.values()
            if session_id is not None:
                candidates = (s for s in candidates if s.session_id == session_id)
            ordered = sorted(
                candidates, key=lambda s: (s.started_at, s.id), reverse=True,
            )
        if before is not None:
            anchor_at, anchor_id = decode_cursor(before)
            anchor = (_as_aware(anchor_at), anchor_id)
            ordered = [
                s for s in ordered if (_as_aware(s.started_at), s.id) < anchor
            ]
        page = ordered[:limit]
        if len(ordered) > limit and page:
            last = page[-1]
            return page, encode_cursor(before=last.started_at, last_id=last.id)
        return page, None
