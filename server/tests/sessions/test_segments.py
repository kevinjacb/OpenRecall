"""Phase 2 — the segment model (spec §2.1).

These tests pin the two properties everything else in Phase 2 and Phase 3
depends on: segments are cut where a person would cut them, and their ids
survive a rebuild so durable metadata stays attached.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from opensapien_server.events.model import CaptureEvent
from opensapien_server.events.store import InMemoryEventStore
from opensapien_server.sessions.segments import (
    SEGMENT_IDLE_MS,
    SegmentIndex,
)

_T0 = datetime(2026, 8, 8, 9, 0, 0, tzinfo=timezone.utc)


def _event(seq: int, *, session_id="s1", at=None, start_ms=None, text="hello", ms=1000):
    return CaptureEvent(
        event_id=f"{session_id}:{seq}",
        session_id=session_id,
        seq=seq,
        kind="transcript",
        created_at=at if at is not None else _T0 + timedelta(seconds=seq),
        text=text,
        duration_ms=ms,
        start_ms=start_ms if start_ms is not None else seq * ms,
    )


# ---- cutting rules ----------------------------------------------------------


def test_consecutive_events_form_one_segment():
    idx = SegmentIndex()
    for seq in range(3):
        idx.record(_event(seq))

    segments = idx.for_session("s1")

    assert len(segments) == 1
    assert segments[0].first_seq == 0
    assert segments[0].last_seq == 2
    assert segments[0].transcript_count == 3


def test_an_idle_gap_starts_a_new_segment():
    idx = SegmentIndex()
    idx.record(_event(0, at=_T0))
    idx.record(_event(1, at=_T0 + timedelta(milliseconds=SEGMENT_IDLE_MS + 1)))

    segments = idx.for_session("s1")

    assert [s.first_seq for s in segments] == [0, 1]
    assert segments[0].closed is True
    assert segments[1].closed is False


def test_a_gap_shorter_than_the_threshold_stays_in_one_segment():
    idx = SegmentIndex()
    idx.record(_event(0, at=_T0))
    idx.record(_event(1, at=_T0 + timedelta(milliseconds=SEGMENT_IDLE_MS - 1)))

    assert len(idx.for_session("s1")) == 1


def test_the_hard_cap_force_closes_a_continuous_segment():
    """A continuously-noisy room would otherwise produce one unbrowsable
    row per day."""
    idx = SegmentIndex(max_ms=5_000)
    idx.record(_event(0, start_ms=0, ms=1000))
    idx.record(_event(1, start_ms=4_500, ms=1000))  # crosses the cap
    idx.record(_event(2, start_ms=5_500, ms=1000))

    assert len(idx.for_session("s1")) == 2


def test_sessions_are_segmented_independently():
    idx = SegmentIndex()
    idx.record(_event(0, session_id="s1"))
    idx.record(_event(0, session_id="s2"))

    assert len(idx.for_session("s1")) == 1
    assert len(idx.for_session("s2")) == 1
    assert idx.total() == 2


def test_the_idle_test_uses_wall_clock_and_the_cap_uses_audio_time():
    """A long reconnect gap must not count against the hour cap, and a long
    silent stretch must not count as recorded length."""
    idx = SegmentIndex(idle_ms=60_000, max_ms=10_000)
    idx.record(_event(0, at=_T0, start_ms=0, ms=1000))
    # 30s of wall clock passes but only 1s of audio: under the idle
    # threshold, and nowhere near the audio cap.
    idx.record(_event(1, at=_T0 + timedelta(seconds=30), start_ms=1000, ms=1000))

    assert len(idx.for_session("s1")) == 1


# ---- ids and rebuild --------------------------------------------------------


def test_segment_ids_are_deterministic_from_session_and_first_seq():
    idx = SegmentIndex()
    idx.record(_event(7))

    assert idx.for_session("s1")[0].id == "s1:7"


def test_a_rebuild_reproduces_identical_ids():
    """Durable titles are keyed by segment id (spec §2.2), so a rebuild that
    renamed segments would silently orphan every title."""
    store = InMemoryEventStore()
    store.append(_event(0, at=_T0))
    store.append(_event(1, at=_T0 + timedelta(seconds=1)))
    # Idle is measured from the segment's *last* event (seq 1, at T0+1s).
    store.append(
        _event(2, at=_T0 + timedelta(seconds=1, milliseconds=SEGMENT_IDLE_MS + 1)),
    )

    live = SegmentIndex()
    for e in store.events("s1"):
        live.record(e)
    rebuilt = SegmentIndex()
    rebuilt.rebuild_from_store(store)

    assert [s.id for s in live.for_session("s1")] == ["s1:0", "s1:2"]
    assert [s.id for s in rebuilt.for_session("s1")] == ["s1:0", "s1:2"]


def test_a_rebuild_does_not_double_count():
    store = InMemoryEventStore()
    store.append(_event(0))
    idx = SegmentIndex()

    idx.rebuild_from_store(store)
    idx.rebuild_from_store(store)

    assert idx.total() == 1
    assert idx.for_session("s1")[0].transcript_count == 1


# ---- idle close -------------------------------------------------------------


def test_close_idle_closes_a_stale_open_segment():
    idx = SegmentIndex()
    idx.record(_event(0, at=_T0))

    closed = idx.close_idle(now=_T0 + timedelta(milliseconds=SEGMENT_IDLE_MS + 1))

    assert [s.id for s in closed] == ["s1:0"]
    assert idx.get("s1:0").closed is True


def test_close_idle_leaves_a_live_segment_open():
    idx = SegmentIndex()
    idx.record(_event(0, at=_T0))

    assert idx.close_idle(now=_T0 + timedelta(seconds=1)) == []
    assert idx.get("s1:0").closed is False


def test_close_idle_is_idempotent():
    idx = SegmentIndex()
    idx.record(_event(0, at=_T0))
    later = _T0 + timedelta(milliseconds=SEGMENT_IDLE_MS + 1)

    assert len(idx.close_idle(now=later)) == 1
    assert idx.close_idle(now=later) == []


def test_close_session_closes_the_open_segment():
    idx = SegmentIndex()
    idx.record(_event(0))

    closed = idx.close_session("s1")

    assert closed.id == "s1:0"
    assert idx.get("s1:0").closed is True


def test_close_session_on_an_unknown_session_returns_none():
    assert SegmentIndex().close_session("nope") is None


def test_an_event_after_a_close_opens_a_new_segment():
    """A reconnect writes into a fresh segment rather than silently
    resurrecting a closed one."""
    idx = SegmentIndex()
    idx.record(_event(0))
    idx.close_session("s1")

    idx.record(_event(1))

    assert [s.id for s in idx.for_session("s1")] == ["s1:0", "s1:1"]


# ---- lookups and paging -----------------------------------------------------


def test_segment_for_event_maps_a_hit_back_to_its_row():
    idx = SegmentIndex()
    for seq in range(3):
        idx.record(_event(seq))

    assert idx.segment_for_event("s1", 2).id == "s1:0"
    assert idx.segment_for_event("s1", 99) is None


def test_list_pages_newest_first_without_repeats():
    idx = SegmentIndex()
    for i in range(5):
        idx.record(
            _event(i * 10, at=_T0 + timedelta(hours=i)),
        )
        idx.close_session("s1")

    seen: list[str] = []
    cursor = None
    while True:
        page, cursor = idx.list(limit=2, before=cursor)
        seen.extend(s.id for s in page)
        if cursor is None:
            break

    assert seen == ["s1:40", "s1:30", "s1:20", "s1:10", "s1:0"]


def test_list_rejects_a_malformed_cursor():
    idx = SegmentIndex()
    idx.record(_event(0))

    try:
        idx.list(limit=2, before="garbage")
    except ValueError:
        return
    raise AssertionError("expected ValueError on a malformed cursor")


def test_delete_removes_the_segment_and_frees_the_session_slot():
    idx = SegmentIndex()
    idx.record(_event(0))

    assert idx.delete("s1:0") is True
    assert idx.get("s1:0") is None
    assert idx.delete("s1:0") is False

    idx.record(_event(1))
    assert [s.id for s in idx.for_session("s1")] == ["s1:1"]
