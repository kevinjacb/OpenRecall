from datetime import datetime, timezone

from openrecall_server.gateway.core import ProactiveOutbox
from openrecall_server.reminders.store import InMemoryReminderStore
from openrecall_server.reminders.sweeper import ReminderSweeper


def _due(minute):
    return datetime(2026, 8, 18, 12, minute, 0, tzinfo=timezone.utc)


def test_sweep_once_fires_due_reminder_into_outbox_and_marks_fired():
    store = InMemoryReminderStore()
    outbox = ProactiveOutbox()
    store.add(atom_id="r1", session_id="s1", text="Call mom", due_at=_due(0))
    sweeper = ReminderSweeper(store=store, outbox=outbox, clock=None)
    fired = sweeper.sweep_once(now=_due(5))
    assert fired == 1
    msgs = outbox.drain("s1")
    assert len(msgs) == 1
    assert "Call mom" in msgs[0].text
    assert msgs[0].session_id == "s1"
    # Idempotent: a second sweep finds nothing (r1 is now fired).
    assert sweeper.sweep_once(now=_due(6)) == 0
    assert outbox.drain("s1") == []


def test_sweep_once_skips_future_reminders():
    store = InMemoryReminderStore()
    outbox = ProactiveOutbox()
    store.add(atom_id="r1", session_id="s1", text="Call mom", due_at=_due(30))
    sweeper = ReminderSweeper(store=store, outbox=outbox, clock=None)
    assert sweeper.sweep_once(now=_due(5)) == 0
    assert outbox.drain("s1") == []


def test_sweep_once_skips_done_reminders():
    store = InMemoryReminderStore()
    outbox = ProactiveOutbox()
    store.add(atom_id="r1", session_id="s1", text="Call mom", due_at=_due(0))
    store.mark_done("r1")
    sweeper = ReminderSweeper(store=store, outbox=outbox, clock=None)
    assert sweeper.sweep_once(now=_due(5)) == 0