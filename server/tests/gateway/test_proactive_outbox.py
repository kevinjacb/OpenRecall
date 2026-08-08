"""Tests for the ProactiveOutbox and the gateway's send_proactive seam (P3)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from opensapien_server.agent.metrics import InMemoryMetricsRecorder
from opensapien_server.contracts.clock import FakeClock
from opensapien_server.contracts.metrics import Metrics
from opensapien_server.gateway.core import ProactiveOutbox
from opensapien_server.protocol.messages import ProactiveMessage


def _msg(session_id: str = "s1", request_id: str = "r1") -> ProactiveMessage:
    return ProactiveMessage(
        session_id=session_id, request_id=request_id, text="hi", atoms=("a1",),
    )


def test_outbox_enqueue_and_drain():
    clock = FakeClock(datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc))
    metrics = InMemoryMetricsRecorder()
    box = ProactiveOutbox(ttl_s=30.0, clock=clock, metrics=metrics)

    box.enqueue(_msg())
    drained = box.drain("s1")
    assert len(drained) == 1
    assert drained[0].text == "hi"


def test_outbox_drain_evicts_expired_and_counts():
    clock = FakeClock(datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc))
    metrics = InMemoryMetricsRecorder()
    box = ProactiveOutbox(ttl_s=30.0, clock=clock, metrics=metrics)

    box.enqueue(_msg())
    # Advance the clock past the TTL.
    clock.advance(31.0)
    drained = box.drain("s1")
    assert drained == []
    total = sum(
        v for (n, tags), v in metrics._counters.items()
        if n == Metrics.PROACTIVE_DELIVERY_DROPPED_TOTAL
        and dict(tags).get("reason") == "ttl_exceeded"
    )
    assert total == 1


def test_outbox_drain_unknown_session_returns_empty():
    clock = FakeClock(datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc))
    box = ProactiveOutbox(ttl_s=30.0, clock=clock)
    assert box.drain("nope") == []


def test_outbox_per_session_buckets_are_independent():
    """A drain for session A doesn't touch session B's bucket."""
    clock = FakeClock(datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc))
    box = ProactiveOutbox(ttl_s=30.0, clock=clock)
    box.enqueue(_msg("s1", "r1"))
    box.enqueue(_msg("s2", "r2"))
    drained_a = box.drain("s1")
    drained_b = box.drain("s2")
    assert [m.session_id for m in drained_a] == ["s1"]
    assert [m.session_id for m in drained_b] == ["s2"]


def test_outbox_wait_and_signal_semantics():
    """wait() blocks until signal() is called, drain() clears the event."""
    import asyncio
    clock = FakeClock(datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc))
    box = ProactiveOutbox(ttl_s=30.0, clock=clock)

    async def run() -> bool:
        # No work pending — wait should block. Race it against a 0.05s timeout.
        try:
            await asyncio.wait_for(box.wait(), timeout=0.05)
        except asyncio.TimeoutError:
            return False
        return True

    # Use a fresh loop for the test.
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(run())
        assert result is False
    finally:
        loop.close()


def test_drain_all_clears_event_after_bare_signal():
    """A bare signal() (e.g. the adapter's connection-close wake) sets the
    event with no enqueued message. drain_all() must clear the event so the
    drain loop's next wait() blocks — otherwise wait() returns immediately
    forever (it does not yield when the event is already set), hot-spinning
    at 100% CPU and starving the event loop. This is the 'server works for
    ~20s then every connection times out, no error in the terminal' incident.
    """
    box = ProactiveOutbox(ttl_s=30.0)
    box.signal()  # bare signal, no message
    assert box._event.is_set()  # the hot-spin precondition
    drained = box.drain_all()
    assert drained == []  # nothing to send
    assert not box._event.is_set()  # cleared → next wait() blocks, no spin


def test_drain_all_returns_and_clears_enqueued_messages():
    """drain_all() drains every pending bucket and clears the event so the
    next wait() blocks until a new enqueue re-sets it."""
    box = ProactiveOutbox(ttl_s=30.0)
    box.enqueue(_msg("s1", "r1"))
    box.enqueue(_msg("s2", "r2"))
    assert box._event.is_set()
    drained = box.drain_all()
    assert {m.session_id for m in drained} == {"s1", "s2"}
    assert not box._event.is_set()  # cleared after draining


def test_drain_all_is_idempotent_when_empty():
    """Calling drain_all() repeatedly on an empty outbox does not re-set the
    event — it stays cleared, so the drain loop blocks."""
    box = ProactiveOutbox(ttl_s=30.0)
    assert box.drain_all() == []
    assert not box._event.is_set()
    assert box.drain_all() == []
    assert not box._event.is_set()
