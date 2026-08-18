import asyncio
from datetime import datetime, timezone

import pytest

from openrecall_server.contracts.clock import FakeClock
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


@pytest.mark.asyncio
async def test_run_delivers_due_reminder_through_loop_to_outbox():
    """Regression guard for the thread-safety fix (Finding #1).

    sweep_once calls outbox.enqueue() -> asyncio.Event.set(), which is NOT
    thread-safe from a worker thread (it uses loop.call_soon, not
    call_soon_threadsafe, so it would not reliably wake the proactive send
    loop's await outbox.wait()). The _run loop must therefore call
    sweep_once directly on the event loop thread, NOT via asyncio.to_thread.
    This test exercises the full _run delivery path through a real loop;
    if someone re-adds to_thread, the enqueue happens off-loop and the
    Event.set() may not wake a waiter — the reminder gets enqueued but
    delivery is unreliable.

    The 0.3s sleep is deliberately generous over the 0.05s interval to
    avoid flakiness on slow CI runners; determinism of the *outcome* (the
    message lands in the outbox) is what matters, not the exact tick count.
    """
    store = InMemoryReminderStore()
    outbox = ProactiveOutbox()
    store.add(atom_id="r1", session_id="s1", text="Call mom", due_at=_due(0))
    # Clock set past the due time so sweep_once() (called with no `now`
    # arg from _run) sees the reminder as due.
    clock = FakeClock(start=_due(5))
    sweeper = ReminderSweeper(
        store=store, outbox=outbox, clock=clock, interval_s=0.05,
    )
    await sweeper.start()
    # Wait long enough for at least one tick to fire. Generous sleep over
    # the 0.05s interval to avoid flakiness; the outcome is deterministic.
    await asyncio.sleep(0.3)
    await sweeper.stop()
    msgs = outbox.drain("s1")
    assert len(msgs) == 1
    assert "Call mom" in msgs[0].text
    assert msgs[0].session_id == "s1"