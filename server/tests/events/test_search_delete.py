"""EventStore.search and delete_range (spec §2.4, §5.2), both backends."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openrecall_server.events.model import CaptureEvent
from openrecall_server.events.store import InMemoryEventStore, SqliteEventStore

_T0 = datetime(2026, 8, 8, 9, 0, 0, tzinfo=timezone.utc)


def _event(seq, *, session_id="s1", text="hello", at=None):
    return CaptureEvent(
        event_id=f"{session_id}:{seq}",
        session_id=session_id,
        seq=seq,
        kind="transcript",
        created_at=at if at is not None else _T0 + timedelta(seconds=seq),
        text=text,
        duration_ms=1000,
        start_ms=seq * 1000,
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryEventStore()
    return SqliteEventStore(tmp_path / "events.db")


# ---- search -----------------------------------------------------------------


def test_search_finds_a_substring(store):
    store.append(_event(0, text="the quarterly budget review"))
    store.append(_event(1, text="lunch plans"))

    hits = store.search("budget")

    assert [e.seq for e in hits] == [0]


def test_search_is_case_insensitive(store):
    store.append(_event(0, text="The Budget Review"))

    assert len(store.search("budget")) == 1
    assert len(store.search("BUDGET")) == 1


def test_search_returns_newest_first(store):
    store.append(_event(0, text="budget one", at=_T0))
    store.append(_event(1, text="budget two", at=_T0 + timedelta(hours=1)))

    assert [e.seq for e in store.search("budget")] == [1, 0]


def test_search_escapes_like_wildcards(store):
    """A user typing `50%` must not match every event."""
    store.append(_event(0, text="revenue up 50% this quarter"))
    store.append(_event(1, text="nothing relevant"))

    assert [e.seq for e in store.search("50%")] == [0]


def test_search_escapes_the_underscore_wildcard(store):
    store.append(_event(0, text="the file is a_b.txt"))
    store.append(_event(1, text="the file is axb.txt"))

    assert [e.seq for e in store.search("a_b")] == [0]


def test_search_for_an_empty_query_returns_nothing(store):
    store.append(_event(0))

    assert store.search("") == []
    assert store.search("   ") == []


def test_search_respects_the_limit(store):
    for seq in range(5):
        store.append(_event(seq, text="budget"))

    assert len(store.search("budget", limit=2)) == 2


def test_search_spans_sessions(store):
    store.append(_event(0, session_id="s1", text="budget"))
    store.append(_event(0, session_id="s2", text="budget"))

    assert {e.session_id for e in store.search("budget")} == {"s1", "s2"}


# ---- delete_range -----------------------------------------------------------


def test_delete_range_removes_only_the_inclusive_range(store):
    for seq in range(5):
        store.append(_event(seq))

    assert store.delete_range("s1", 1, 3) == 3

    assert [e.seq for e in store.events("s1")] == [0, 4]


def test_delete_range_is_idempotent(store):
    store.append(_event(0))

    assert store.delete_range("s1", 0, 0) == 1
    assert store.delete_range("s1", 0, 0) == 0


def test_delete_range_leaves_other_sessions_alone(store):
    store.append(_event(0, session_id="s1"))
    store.append(_event(0, session_id="s2"))

    store.delete_range("s1", 0, 0)

    assert len(store.events("s2")) == 1


def test_delete_range_on_an_unknown_session_is_a_noop(store):
    assert store.delete_range("nope", 0, 10) == 0


def test_a_deleted_event_id_can_be_appended_again(store):
    """Delete must actually free the id, not leave a tombstone that makes
    the segment un-recreatable if the same events are re-ingested."""
    store.append(_event(0))
    store.delete_range("s1", 0, 0)

    assert store.append(_event(0)) is True
