"""In-memory per-session summary index.

The :class:`SessionIndex` is the server-side view of "what sessions exist and
what do we know about them" — the per-session aggregates that power the
``/sessions`` HTTP routes. It is *not* the source of truth for the event
timeline; that lives in the :class:`~opensapien_server.events.store.EventStore`.
The index is derived state, kept in sync via :meth:`record` after every
successful ``EventStore.append``.

Design notes:

* **Thread-safe.** The gateway offloads ingest to a worker thread
  (``serve()`` uses ``asyncio.to_thread``); the HTTP handlers run on the
  aiohttp event loop. We use a single ``threading.Lock`` around the
  internal dict (mirroring the pattern in :class:`SqliteEventStore`).
* **Clock injection.** The ``clock`` parameter defaults to UTC now; tests
  pass a controllable clock so the 24h-window counter is deterministic.
* **Cursor format.** The :meth:`list` method's cursor is an opaque
  base64-encoded JSON ``{"before": iso, "last_id": sid}`` payload. The
  HTTP route layer is the boundary that encodes/decodes it; the index
  itself works in ``(before, last_id)`` pairs so the inner format is
  decoupled from the wire format.
* **Preview truncation.** :data:`PREVIEW_MAX_CHARS` bounds the preview to
  a UI-friendly length; long transcripts get an ellipsis suffix.
"""
from __future__ import annotations

import base64
import collections
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from ..events.model import CaptureEvent
from ..events.store import EventStore  # only used for the type annotation in rebuild_from_store

Clock = Callable[[], datetime]
PREVIEW_MAX_CHARS = 80
_CURSOR_TOO_OLD = object()  # sentinel for "before cursor runs off the start"


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SessionSummary:
    """One session's view-model aggregates.

    Wire-format mirror of the Android
    ``com.opensapien.relay.http.dto.SessionSummaryDto``. Field names use Python
    snake_case; the HTTP layer camelCases them on the way out.
    """

    id: str
    started_at: datetime
    ended_at: datetime | None
    event_count: int
    transcript_count: int
    preview: str | None
    # Internal flag: True once the gateway's lifecycle registry has recorded
    # a Bye for this session. The HTTP layer exposes this to the wire as
    # `endedAt`; the index itself only knows that *some* lifecycle event
    # marked the session closed.
    _closed: bool = field(default=False, repr=False, compare=False)

    def mark_closed(self, at: datetime) -> None:
        """Record that the session has been closed at ``at``.

        Idempotent: the first close wins, subsequent calls (e.g. a second
        Bye from a replay) do not move ``ended_at`` backwards.
        """
        if self._closed:
            return
        self._closed = True
        self.ended_at = at

    def duration_ms(self, *, now: datetime | None = None) -> int:
        """Server-side view of session length: started_at..ended_at (or now)."""
        end = self.ended_at if self._closed else (now or _default_clock())
        if end < self.started_at:
            return 0
        return int((end - self.started_at).total_seconds() * 1000)


def _truncate(text: str, limit: int = PREVIEW_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _preview_of(text: str) -> str | None:
    """First non-empty/whitespace transcript text, truncated; or None."""
    stripped = text.strip()
    if not stripped:
        return None
    return _truncate(stripped)


def _encode_cursor(*, before: datetime, last_id: str) -> str:
    payload = json.dumps({"before": before.isoformat(), "last_id": last_id})
    # Strip trailing `=` padding: `=` is URL-special per RFC 3986 and the
    # brief treats the cursor as opaque. A future client URL-encoding the
    # cursor into a query string would otherwise risk a 1-char DoS on `=`.
    return base64.urlsafe_b64encode(payload.encode("utf-8")).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode a base64 JSON cursor. Raises :class:`ValueError` on malformed input."""
    try:
        # Re-pad to a multiple of 4 — Python's b64 decoder is strict about
        # padding and the encoder strips it.
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        before = datetime.fromisoformat(payload["before"])
        last_id = str(payload["last_id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"bad cursor: {e!s}") from e
    return before, last_id


class SessionIndex:
    """Thread-safe in-memory per-session summary index.

    Mutators: :meth:`record` (called by ``GatewayCore`` after every successful
    ``EventStore.append``) and :meth:`mark_closed` (called by
    ``SessionLifecycle`` when a connection's Bye is processed).

    Readers: :meth:`summary`, :meth:`list`, :meth:`total_sessions`,
    :meth:`recent_events_24h`, :meth:`preview_text`.
    """

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._summaries: dict[str, SessionSummary] = {}
        self._event_count: int = 0
        # Per-event timestamp deque for the rolling 24h counter. Append-only
        # under the lock; pruned on read against the injected clock.
        self._recent_event_timestamps: collections.deque[datetime] = collections.deque()
        self._lock = threading.Lock()
        self._clock: Clock = clock or _default_clock

    # -- mutators -------------------------------------------------------------

    def record(self, event: CaptureEvent) -> None:
        """Fold one newly-stored event into the per-session summary.

        Sessions are keyed by ``event.session_id``; the summary is created
        on the first event for that session, with ``started_at`` taken from
        that event's ``created_at``.
        """
        with self._lock:
            self._event_count += 1
            self._recent_event_timestamps.append(event.created_at)
            existing = self._summaries.get(event.session_id)
            if existing is None:
                self._summaries[event.session_id] = SessionSummary(
                    id=event.session_id,
                    started_at=event.created_at,
                    ended_at=None,
                    event_count=1,
                    transcript_count=1 if event.kind == "transcript" else 0,
                    preview=_preview_of(event.text) if event.kind == "transcript" else None,
                )
                return
            existing.event_count += 1
            if event.kind == "transcript":
                existing.transcript_count += 1
                if existing.preview is None:
                    new_preview = _preview_of(event.text)
                    if new_preview is not None:
                        existing.preview = new_preview

    def rebuild_from_store(self, event_store: "EventStore") -> None:
        """Cold-start rebuild from a durable :class:`EventStore`.

        Replays every event in ``event_store`` through :meth:`record`
        so a fresh :class:`SessionIndex` has the same summaries the
        live path produced. Idempotent: re-running produces identical
        state because we wipe the index first (rebuilding onto a
        non-empty index would double-count). Failure-isolated: a
        per-session error is logged and the rebuild continues with
        the next session; a bad event in the durable store must not
        block every other session.

        Performance: a 100-session history rebuild reads N events
        per session and folds them under the existing lock; budget
        is ~2ms per session. Acceptable for a one-shot startup pass.
        """
        import logging
        log = logging.getLogger(__name__)
        # Cold-start semantics: a rebuild is a "start fresh from the
        # durable store" operation. Clear the existing summaries so
        # re-running against the same store (test harness, double
        # restart) does not double-count events. We hold the same
        # lock as record() so a concurrent live append either lands
        # before the wipe (and is wiped) or after (and survives).
        with self._lock:
            self._summaries.clear()
            self._event_count = 0
            self._recent_event_timestamps.clear()
        for sid in sorted(event_store.sessions()):
            try:
                for ev in event_store.events(sid):
                    self.record(ev)
            except Exception:
                # Defensive: one bad session in the durable store
                # must not poison the whole rebuild. The live path
                # will re-populate the missing summary on the next
                # event append.
                log.exception(
                    "session_index_rebuild_session_failed",
                    extra={"session_id": sid},
                )
                continue

    # -- readers --------------------------------------------------------------

    def summary(self, session_id: str) -> SessionSummary | None:
        with self._lock:
            return self._summaries.get(session_id)

    def total_sessions(self) -> int:
        with self._lock:
            return len(self._summaries)

    def recent_events_24h(self) -> int:
        """Count events whose ``created_at`` is strictly within the last 24h.

        "Strictly within 24h" means *newer than* the (now - 24h) timestamp:
        an event exactly 24h old is **not** included. The counter is
        per-event (not per-session) — a session that started 25h ago with a
        fresh event 1h ago counts as 1, and a session that started 1h ago
        with 100 events counts as 100. The buffer holds event timestamps
        in append-order; pruning at read time is a full O(K) scan that
        drops out-of-window entries, so the buffer doesn't grow
        unboundedly across long-running sessions.
        """
        cutoff = self._clock() - _ONE_DAY
        with self._lock:
            timestamps = self._recent_event_timestamps
            # Filter in place: keep only entries strictly newer than cutoff.
            # The buffer is bounded by the rolling 24h window in practice,
            # so the scan is cheap.
            kept = [ts for ts in timestamps if ts > cutoff]
            timestamps.clear()
            timestamps.extend(kept)
            return len(kept)

    def preview_text(self, session_id: str) -> str | None:
        s = self.summary(session_id)
        return s.preview if s is not None else None

    def list(
        self, *, limit: int, before: str | None = None
    ) -> tuple[list[SessionSummary], str | None]:
        """Return up to ``limit`` summaries ordered by ``started_at`` desc.

        ``before`` is the opaque cursor returned by a prior call; ``None``
        starts at the most recent session. Returns a cursor for the next
        page, or ``None`` when the result was the final page.
        """
        if limit <= 0:
            raise ValueError(f"limit must be positive, got {limit}")
        with self._lock:
            ordered = sorted(
                self._summaries.values(),
                key=lambda s: (s.started_at, s.id),
                reverse=True,
            )
            if before is not None:
                cursor_before, cursor_last_id = _decode_cursor(before)
                # skip past everything strictly after the cursor anchor
                # (newer than `before`, or same instant with id > last_id)
                start_idx = 0
                for i, s in enumerate(ordered):
                    if s.started_at < cursor_before or (
                        s.started_at == cursor_before and s.id < cursor_last_id
                    ):
                        start_idx = i
                        break
                else:
                    # cursor ran off the end of the data
                    return [], None
                ordered = ordered[start_idx:]

            page = ordered[:limit]
            if len(ordered) > limit:
                # The last item in `page` is the cursor anchor; the next
                # call resumes strictly *after* it.
                anchor = page[-1]
                next_cursor = _encode_cursor(before=anchor.started_at, last_id=anchor.id)
                return list(page), next_cursor
            return list(page), None


_ONE_DAY = __import__("datetime").timedelta(days=1)
