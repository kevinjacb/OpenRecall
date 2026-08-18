from datetime import datetime, timezone

import pytest

from openrecall_server.reminders.store import (
    InMemoryReminderStore, Reminder, SqliteReminderStore,
)


def _due(minute):
    return datetime(2026, 8, 18, 12, minute, 0, tzinfo=timezone.utc)


def _add(store, atom_id="a1", due=_due(0), text="call mom", session_id="s1"):
    return store.add(
        atom_id=atom_id, session_id=session_id, text=text, due_at=due,
    )


def test_add_is_idempotent_by_atom_id(store):
    assert _add(store) is True
    assert _add(store) is False  # same atom_id


def test_due_returns_only_past_due_pending(store):
    now = _due(5)
    _add(store, atom_id="past", due=_due(0))
    _add(store, atom_id="future", due=_due(30))
    due = store.due(now)
    assert [r.atom_id for r in due] == ["past"]


def test_due_excludes_fired_and_done(store):
    now = _due(5)
    _add(store, atom_id="r1", due=_due(0))
    store.mark_fired("r1", fired_at=now)
    _add(store, atom_id="r2", due=_due(0))
    store.mark_done("r2")
    assert store.due(now) == []


def test_mark_fired_is_idempotent(store):
    now = _due(5)
    _add(store, atom_id="r1", due=_due(0))
    store.mark_fired("r1", fired_at=now)
    store.mark_fired("r1", fired_at=now)  # no error
    r = store.get("r1")
    assert r.status == "fired"
    assert r.fired_at == now


def test_list_pending_and_all(store):
    _add(store, atom_id="r1", due=_due(0))
    _add(store, atom_id="r2", due=_due(30))
    store.mark_done("r2")
    assert {r.atom_id for r in store.list(only_pending=True)} == {"r1"}
    assert {r.atom_id for r in store.list(only_pending=False)} == {"r1", "r2"}


def test_mark_done_returns_false_for_unknown(store):
    assert store.mark_done("nope") is False


def test_due_returns_ascending_by_due_at(store):
    # Insert in NON-chronological order: later-due first, earlier-due second.
    now = _due(45)
    _add(store, atom_id="later", due=_due(30))
    _add(store, atom_id="earlier", due=_due(5))
    due = store.due(now)
    assert [r.atom_id for r in due] == ["earlier", "later"]


def test_list_returns_ascending_by_due_across_statuses(store):
    # Mixed statuses, inserted out of due_at order; both branches must
    # return ascending by due_at, matching the SQLite twin's ORDER BY.
    _add(store, atom_id="late_done", due=_due(40))
    _add(store, atom_id="early_pending", due=_due(5))
    _add(store, atom_id="mid_fired", due=_due(20))
    store.mark_fired("mid_fired", fired_at=_due(25))
    store.mark_done("late_done")
    pending = store.list(only_pending=True)
    assert [r.atom_id for r in pending] == ["early_pending"]
    all_rows = store.list(only_pending=False)
    assert [r.atom_id for r in all_rows] == ["early_pending", "mid_fired", "late_done"]


@pytest.fixture(params=[InMemoryReminderStore, "sqlite"])
def store(request, tmp_path):
    if request.param == "sqlite":
        return SqliteReminderStore(tmp_path / "reminders.db")
    return InMemoryReminderStore()