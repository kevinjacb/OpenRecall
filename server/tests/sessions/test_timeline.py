from openrecall_server.sessions.timeline import SessionTimelineIndex


def test_record_and_resolve_by_rel_ts_range():
    tl = SessionTimelineIndex()
    tl.record("s1", 1000)
    tl.record("s1", 5000)
    tl.record("s2", 10000)
    tl.record("s2", 20000)
    assert tl.session_for_rel_ts(3000) == "s1"
    assert tl.session_for_rel_ts(15000) == "s2"
    assert tl.rel_range("s1") == (1000, 5000)


def test_unmatched_rel_ts_returns_none():
    tl = SessionTimelineIndex()
    tl.record("s1", 1000)
    tl.record("s1", 5000)
    assert tl.session_for_rel_ts(99999) is None
    assert tl.rel_range("nope") is None


def test_empty_timeline_resolves_none():
    assert SessionTimelineIndex().session_for_rel_ts(0) is None


def test_overlapping_ranges_pick_most_recent():
    # single-boot sim won't overlap, but define deterministic behavior:
    # the session with the latest rel_ts_min among containing ranges wins.
    tl = SessionTimelineIndex()
    tl.record("s1", 0)
    tl.record("s1", 10000)
    tl.record("s2", 2000)
    tl.record("s2", 8000)
    assert tl.session_for_rel_ts(5000) == "s2"   # s2 has later min