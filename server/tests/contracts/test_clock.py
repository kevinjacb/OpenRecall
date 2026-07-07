"""Tests for the Clock Protocol and its two reference implementations.

The Clock is the single seam for "what time is it?" — production uses
:class:`SystemClock`; tests inject :class:`FakeClock` for deterministic
timestamps in audit/retrieval results.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sense_server.contracts.clock import FakeClock, SystemClock


def test_system_clock_returns_utc_now():
    now = SystemClock().now()
    assert now.tzinfo == timezone.utc
    # Within a sane window of wall-clock now.
    delta = abs((datetime.now(timezone.utc) - now).total_seconds())
    assert delta < 5


def test_fake_clock_starts_at_given_time():
    start = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)
    assert FakeClock(start).now() == start


def test_fake_clock_advances_by_seconds():
    start = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)
    c = FakeClock(start)
    c.advance(60)
    assert c.now() == start + timedelta(seconds=60)
    c.advance(3600)
    assert c.now() == start + timedelta(seconds=60 + 3600)


def test_fake_clock_set_now_jumps_directly():
    start = datetime(2026, 7, 7, tzinfo=timezone.utc)
    c = FakeClock(start)
    c.set_now(datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert c.now() == datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_fake_clock_default_returns_epoch():
    c = FakeClock()
    assert c.now() == datetime(1970, 1, 1, tzinfo=timezone.utc)
