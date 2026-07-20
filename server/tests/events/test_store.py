"""Tests for the idempotent, append-only capture-event store.

Capture events (§F) are the durable, immutable record of what the wearable
captured. Delivery upstream is at-least-once, so the store must be **idempotent by
event_id**: re-appending the same event is a no-op. Events read back **per session,
in seq order**, regardless of append order (crash-recovery replays may arrive out
of order).

The same suite runs against every EventStore implementation (in-memory + sqlite)
via the ``store`` fixture, so durability backends can't silently diverge.
"""

from datetime import datetime, timezone

import pytest

from sense_server.events.model import CaptureEvent
from sense_server.events.store import InMemoryEventStore, SqliteEventStore


def ev(session_id: str, seq: int, text: str = "x") -> CaptureEvent:
    return CaptureEvent(
        event_id=f"{session_id}:{seq}",
        session_id=session_id,
        seq=seq,
        kind="transcript",
        created_at=datetime(2026, 6, 30, 12, 0, seq, tzinfo=timezone.utc),
        text=text,
        duration_ms=5000,
        start_ms=seq * 5000,
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryEventStore()
    return SqliteEventStore(tmp_path / "events.db")


def test_append_new_event_is_stored_and_readable(store):
    assert store.append(ev("s1", 0, "hello")) is True

    events = store.events("s1")
    assert len(events) == 1
    assert events[0].text == "hello"
    assert events[0].event_id == "s1:0"


def test_append_is_idempotent_by_event_id(store):
    assert store.append(ev("s1", 0, "hello")) is True
    assert store.append(ev("s1", 0, "hello")) is False  # duplicate -> no-op

    assert len(store.events("s1")) == 1


def test_events_are_returned_per_session_in_seq_order(store):
    # append out of seq order on purpose
    store.append(ev("s1", 2))
    store.append(ev("s1", 0))
    store.append(ev("s2", 0))
    store.append(ev("s1", 1))

    assert [e.seq for e in store.events("s1")] == [0, 1, 2]
    assert [e.seq for e in store.events("s2")] == [0]


def test_unknown_session_reads_empty(store):
    assert store.events("nope") == []


def test_sessions_returns_distinct_session_ids(store):
    """Reconcile-on-start (M4.3 reconciliation) needs the EventStore to
    enumerate every session id it holds. Both backends must agree on the
    shape: a list of distinct session ids, order-independent (test sorts
    to be deterministic)."""
    store.append(ev("s1", 0))
    store.append(ev("s1", 1))
    store.append(ev("s2", 0))
    store.append(ev("s3", 0))
    store.append(ev("s3", 1))
    store.append(ev("s3", 2))
    assert sorted(store.sessions()) == ["s1", "s2", "s3"]


def test_sessions_is_empty_for_unwritten_store(store):
    assert store.sessions() == []


def test_sessions_dedupes_even_with_many_events(store):
    """Many events per session still produce one session id per session."""
    for i in range(50):
        store.append(ev("s1", i))
    for i in range(50):
        store.append(ev("s2", i))
    assert sorted(store.sessions()) == ["s1", "s2"]


def test_sessions_survives_persistence_across_reopen(tmp_path):
    """The SqliteEventStore's sessions() must read committed state —
    a fresh handle on the same file sees the same set of sessions."""
    path = tmp_path / "events.db"
    s1 = SqliteEventStore(path)
    s1.append(ev("s1", 0))
    s1.append(ev("s2", 0))

    s2 = SqliteEventStore(path)
    assert sorted(s2.sessions()) == ["s1", "s2"]


def test_sqlite_store_is_usable_from_another_thread(tmp_path):
    # The gateway offloads ingest to worker threads, so the store (built on the main
    # thread) must tolerate access from a different thread.
    import threading

    store = SqliteEventStore(tmp_path / "events.db")
    errors: list[Exception] = []

    def worker() -> None:
        try:
            store.append(ev("s1", 0, "from-thread"))
        except Exception as exc:  # noqa: BLE001 - surfacing any thread-affinity error
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert errors == []
    assert [e.text for e in store.events("s1")] == ["from-thread"]


def test_sqlite_store_persists_across_reopen(tmp_path):
    path = tmp_path / "events.db"
    s1 = SqliteEventStore(path)
    s1.append(ev("s1", 0, "durable"))

    # a fresh handle on the same file must see the committed event
    s2 = SqliteEventStore(path)
    events = s2.events("s1")
    assert len(events) == 1
    assert events[0].text == "durable"
