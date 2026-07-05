"""Tests for the in-memory SessionIndex.

`SessionIndex` is the per-session summary that backs the HTTP `/sessions`
routes. It is fed by `GatewayCore` after every successful `EventStore.append`,
and it derives the per-session aggregates (`start_at`, `event_count`,
`transcript_count`, `preview`) from the event stream itself — the index
holds *no* state of its own beyond those aggregates.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sense_server.events.model import CaptureEvent
from sense_server.sessions.index import SessionIndex, SessionSummary


# ---- helpers ----------------------------------------------------------------


def _ce(
    session_id: str,
    seq: int,
    text: str = "hello",
    created_at: datetime | None = None,
) -> CaptureEvent:
    """Build a §F capture event with sensible defaults for tests."""
    return CaptureEvent(
        event_id=f"{session_id}:{seq}",
        session_id=session_id,
        seq=seq,
        kind="transcript",
        created_at=created_at or datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc),
        text=text,
        duration_ms=100,
        start_ms=seq * 100,
    )


# ---- record() ---------------------------------------------------------------


def test_record_creates_a_new_summary_on_the_first_event():
    idx = SessionIndex()
    idx.record(_ce("s1", 0, text="first"))

    summary = idx.summary("s1")
    assert summary is not None
    assert summary.id == "s1"
    assert summary.event_count == 1
    assert summary.transcript_count == 1
    assert summary.preview == "first"


def test_subsequent_records_increment_event_count():
    idx = SessionIndex()
    idx.record(_ce("s1", 0, text="a"))
    idx.record(_ce("s1", 1, text="b"))
    idx.record(_ce("s1", 2, text="c"))

    assert idx.summary("s1").event_count == 3
    assert idx.summary("s1").transcript_count == 3


def test_record_sets_preview_to_first_non_empty_transcript():
    idx = SessionIndex()
    idx.record(_ce("s1", 0, text=""))  # empty -> no preview
    idx.record(_ce("s1", 1, text="   "))  # whitespace -> no preview
    idx.record(_ce("s1", 2, text="real preview"))

    assert idx.summary("s1").preview == "real preview"


def test_record_keeps_the_first_preview_on_later_events():
    idx = SessionIndex()
    idx.record(_ce("s1", 0, text="first"))
    idx.record(_ce("s1", 1, text="second"))

    assert idx.summary("s1").preview == "first"


def test_record_uses_first_event_created_at_as_started_at():
    idx = SessionIndex()
    first = datetime(2026, 7, 1, 9, 0, 0, tzinfo=timezone.utc)
    later = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    idx.record(_ce("s1", 0, text="a", created_at=first))
    idx.record(_ce("s1", 1, text="b", created_at=later))

    assert idx.summary("s1").started_at == first


def test_summary_returns_none_for_unknown_session():
    idx = SessionIndex()
    assert idx.summary("nope") is None


# ---- total_sessions / recent_events_24h -------------------------------------


def test_total_sessions_counts_distinct_session_ids():
    idx = SessionIndex()
    idx.record(_ce("s1", 0))
    idx.record(_ce("s2", 0))
    idx.record(_ce("s1", 1))  # duplicate session_id
    idx.record(_ce("s3", 0))

    assert idx.total_sessions() == 3


def test_recent_events_24h_uses_injected_clock():
    """`recent_events_24h` must be time-injected so tests don't depend on wall-clock."""
    fixed_now = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

    def clock() -> datetime:
        return fixed_now

    idx = SessionIndex(clock=clock)
    # 1 event exactly 24h ago -> not counted (we want strictly within 24h)
    idx.record(
        _ce("s1", 0, created_at=fixed_now - timedelta(hours=24))
    )
    # 1 event 23h59m ago -> counted
    idx.record(
        _ce("s2", 0, created_at=fixed_now - timedelta(hours=23, minutes=59))
    )
    # 1 event 25h ago -> not counted
    idx.record(
        _ce("s3", 0, created_at=fixed_now - timedelta(hours=25))
    )

    assert idx.recent_events_24h() == 1


def test_recent_events_24h_default_clock_uses_utcnow():
    """Without an injected clock, recent_events_24h uses datetime.now(timezone.utc)."""
    idx = SessionIndex()
    # a fresh event is well within 24h
    idx.record(
        _ce("s1", 0, created_at=datetime.now(timezone.utc))
    )
    assert idx.recent_events_24h() == 1


# ---- list() / pagination ----------------------------------------------------


def _record_sessions(idx: SessionIndex, n: int, base: datetime) -> None:
    """Record `n` distinct sessions with monotonic `started_at` so list() is deterministic."""
    for i in range(n):
        idx.record(
            _ce(f"s{i:03d}", 0, text=f"session {i}", created_at=base + timedelta(minutes=i))
        )


def test_list_returns_summaries_ordered_by_started_at_desc():
    base = datetime(2026, 7, 1, 8, 0, 0, tzinfo=timezone.utc)
    idx = SessionIndex()
    _record_sessions(idx, 5, base)

    sessions, cursor = idx.list(limit=5)
    assert cursor is None  # only 5, requested 5 -> end of list
    assert [s.id for s in sessions] == ["s004", "s003", "s002", "s001", "s000"]


def test_list_with_limit_under_total_returns_cursor_for_next_page():
    base = datetime(2026, 7, 1, 8, 0, 0, tzinfo=timezone.utc)
    idx = SessionIndex()
    _record_sessions(idx, 25, base)

    page1, cursor1 = idx.list(limit=10)
    assert len(page1) == 10
    assert cursor1 is not None
    assert [s.id for s in page1] == [f"s{i:03d}" for i in range(24, 14, -1)]


def test_list_paginates_via_cursor():
    base = datetime(2026, 7, 1, 8, 0, 0, tzinfo=timezone.utc)
    idx = SessionIndex()
    _record_sessions(idx, 25, base)

    page1, cursor1 = idx.list(limit=10)
    page2, cursor2 = idx.list(limit=10, before=cursor1)
    page3, cursor3 = idx.list(limit=10, before=cursor2)

    assert [s.id for s in page1] == [f"s{i:03d}" for i in range(24, 14, -1)]
    assert [s.id for s in page2] == [f"s{i:03d}" for i in range(14, 4, -1)]
    assert [s.id for s in page3] == [f"s{i:03d}" for i in range(4, -1, -1)]
    assert cursor3 is None  # last page is the last page


def test_list_empty_index_returns_empty_list_and_no_cursor():
    idx = SessionIndex()
    sessions, cursor = idx.list(limit=10)
    assert sessions == []
    assert cursor is None


def test_list_with_limit_larger_than_total_returns_all():
    base = datetime(2026, 7, 1, 8, 0, 0, tzinfo=timezone.utc)
    idx = SessionIndex()
    _record_sessions(idx, 3, base)

    sessions, cursor = idx.list(limit=50)
    assert len(sessions) == 3
    assert cursor is None


def test_list_with_invalid_cursor_raises_value_error():
    """The route layer translates ValueError -> 400, but the index raises a clear signal."""
    idx = SessionIndex()
    idx.record(_ce("s1", 0))
    with pytest.raises(ValueError):
        idx.list(limit=10, before="not-a-real-cursor")


# ---- preview truncation -----------------------------------------------------


def test_preview_truncates_long_transcripts_to_about_80_chars():
    idx = SessionIndex()
    long_text = "a" * 200
    idx.record(_ce("s1", 0, text=long_text))

    preview = idx.summary("s1").preview
    assert len(preview) <= 100  # truncated + ellipsis at most
    assert preview.endswith("…") or len(preview) == 80


def test_preview_text_returns_first_non_empty_or_none():
    idx = SessionIndex()
    idx.record(_ce("s1", 0, text=""))
    idx.record(_ce("s1", 1, text="real"))

    assert idx.preview_text("s1") == "real"
    assert idx.preview_text("unknown") is None


# ---- recent_events_24h event-level (per-event 24h bucket) --------------------


def test_recent_events_24h_counts_per_event_not_per_session():
    """Per the brief: count of *events* whose created_at is within the last 24h.

    A session that started >24h ago but had a fresh event should still be counted.
    A session that started within 24h but had >1 event should count as >1.
    """
    fixed_now = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

    def clock() -> datetime:
        return fixed_now

    idx = SessionIndex(clock=clock)
    # Session started 25h ago, but had a fresh event 1h ago -> 1 event counted
    idx.record(_ce("old", 0, created_at=fixed_now - timedelta(hours=25)))
    idx.record(_ce("old", 1, created_at=fixed_now - timedelta(hours=1)))
    # Session started 1h ago, 3 events in the last hour -> 3 events counted
    idx.record(_ce("new", 0, created_at=fixed_now - timedelta(minutes=50)))
    idx.record(_ce("new", 1, created_at=fixed_now - timedelta(minutes=40)))
    idx.record(_ce("new", 2, created_at=fixed_now - timedelta(minutes=30)))

    assert idx.recent_events_24h() == 4


def test_recent_events_24h_drops_out_of_window_on_subsequent_reads():
    """The rolling-window deque is pruned at read time against the injected clock.

    Advancing the clock past 24h must drop the prior events from the bucket.
    """
    t0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    # Mutable clock we can advance from the test.
    now = [t0]

    def clock() -> datetime:
        return now[0]

    idx = SessionIndex(clock=clock)
    idx.record(_ce("s1", 0, created_at=t0))
    idx.record(_ce("s1", 1, created_at=t0))

    assert idx.recent_events_24h() == 2

    # Move the clock past 24h: both events should drop out of the window.
    now[0] = t0 + timedelta(hours=25)
    assert idx.recent_events_24h() == 0

    # A fresh event after the advance IS counted.
    idx.record(_ce("s2", 0, created_at=now[0]))
    assert idx.recent_events_24h() == 1


# ---- cursor: unpadded base64 (Finding 2) -------------------------------------


def test_cursor_encoded_is_unpadded_base64():
    """The encoded cursor must not contain `=` padding — `=` is URL-special."""
    base = datetime(2026, 7, 1, 8, 0, 0, tzinfo=timezone.utc)
    idx = SessionIndex()
    _record_sessions(idx, 5, base)

    # Force a non-None cursor (limit=2 with 5 sessions -> pagination cursor).
    _, cursor = idx.list(limit=2)
    assert cursor is not None
    assert "=" not in cursor


def test_cursor_round_trip_preserves_payload():
    """An unpadded encoded cursor must still decode to its (before, last_id) payload."""
    base = datetime(2026, 7, 1, 8, 0, 0, tzinfo=timezone.utc)
    idx = SessionIndex()
    _record_sessions(idx, 25, base)

    page1, cursor1 = idx.list(limit=10)
    assert cursor1 is not None
    # The unpadded cursor must still drive the second page correctly.
    page2, cursor2 = idx.list(limit=10, before=cursor1)
    assert [s.id for s in page1] == [f"s{i:03d}" for i in range(24, 14, -1)]
    assert [s.id for s in page2] == [f"s{i:03d}" for i in range(14, 4, -1)]


# ---- preview: strip leading/trailing whitespace (Finding 5) -----------------


def test_preview_strips_leading_and_trailing_whitespace():
    """`_preview_of` should return the stripped text, not the raw text."""
    idx = SessionIndex()
    idx.record(_ce("s1", 0, text="  hello world  "))

    preview = idx.summary("s1").preview
    assert preview == "hello world"


def test_preview_strips_whitespace_before_truncation():
    """A padded-but-otherwise-short text should not include the padding in the preview."""
    idx = SessionIndex()
    idx.record(_ce("s1", 0, text="   short   "))

    preview = idx.summary("s1").preview
    assert preview == "short"
