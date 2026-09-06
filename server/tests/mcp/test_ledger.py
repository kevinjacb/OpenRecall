import pytest
from datetime import datetime, timedelta, timezone

from openrecall_server.mcp.ledger import LedgerClosedError, RequestLedger


class FakeClock:
    def __init__(self, now): self._now = now
    def now(self): return self._now
    def advance(self, seconds): self._now += timedelta(seconds=seconds)


def _ledger():
    return RequestLedger(FakeClock(datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)))


def test_open_then_get_returns_entry():
    led = _ledger()
    led.open("r1", session_id="s1", trigger_kind="user_request")
    assert led.get("r1").session_id == "s1"


def test_unknown_request_id_is_none():
    assert _ledger().get("nope") is None


def test_closed_request_id_is_none():
    led = _ledger()
    led.open("r1", session_id=None, trigger_kind="user_request")
    led.close("r1")
    assert led.get("r1") is None


def test_expired_request_id_is_none():
    clock = FakeClock(datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc))
    led = RequestLedger(clock)
    led.open("r1", session_id=None, trigger_kind="user_request", ttl_s=60)
    clock.advance(61)
    assert led.get("r1") is None


def test_recorded_atoms_accumulate_across_calls():
    led = _ledger()
    led.open("r1", session_id=None, trigger_kind="user_request")
    led.record_atoms("r1", ["a1", "a2"])
    led.record_atoms("r1", ["a2", "a3"])
    assert led.cited("r1") == frozenset({"a1", "a2", "a3"})


def test_record_on_closed_request_raises():
    led = _ledger()
    led.open("r1", session_id=None, trigger_kind="user_request")
    led.close("r1")
    with pytest.raises(LedgerClosedError):
        led.record_atoms("r1", ["a1"])


def test_proactive_request_may_not_issue_command():
    """Carries planner.py's PROACTIVE_TRIGGER_CANNOT_ISSUE_COMMAND across
    the process boundary (spec §2 invariant 1)."""
    led = _ledger()
    led.open("r1", session_id="s1", trigger_kind="proactive")
    led.open("r2", session_id="s1", trigger_kind="user_request")
    assert led.may_issue_command("r1") is False
    assert led.may_issue_command("r2") is True


def test_unknown_request_may_not_issue_command():
    assert _ledger().may_issue_command("ghost") is False
