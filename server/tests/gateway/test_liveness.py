"""Phase 5 — device liveness (spec §5.1, D9).

The connection-freshness signal is the part of `/device/status` that is
genuinely measured, so these tests pin what it actually claims.
"""
from __future__ import annotations

from datetime import datetime, timezone

from openrecall_server.gateway.liveness import DeviceLiveness


class Ticker:
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _liveness(tick=None, wall=None):
    return DeviceLiveness(
        monotonic=tick or Ticker(),
        wall=wall or (lambda: datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)),
    )


# ---- never-heard-from vs heard-from-long-ago --------------------------------


def test_a_fresh_device_has_never_been_heard_from():
    """`None` is a distinct answer from "a long time ago" — the header must
    not conflate them."""
    snap = _liveness().snapshot()

    assert snap.connected is False
    assert snap.last_packet_at is None
    assert snap.last_packet_age_s is None
    assert snap.last_transcript_at is None


# ---- connection tracking ----------------------------------------------------


def test_an_open_connection_reads_as_connected():
    live = _liveness()
    live.connection_opened()

    assert live.snapshot().connected is True


def test_a_closed_connection_reads_as_disconnected():
    live = _liveness()
    live.connection_opened()
    live.connection_closed()

    assert live.snapshot().connected is False


def test_an_unmatched_close_cannot_drive_the_count_negative():
    """A duplicated `finally` must not make `connected` permanently false."""
    live = _liveness()
    live.connection_closed()
    live.connection_closed()
    live.connection_opened()

    assert live.snapshot().connected is True


def test_connected_stays_true_while_any_connection_is_open():
    live = _liveness()
    live.connection_opened()
    live.connection_opened()
    live.connection_closed()

    assert live.snapshot().connected is True


# ---- packet freshness -------------------------------------------------------


def test_a_packet_stamps_both_clocks():
    tick = Ticker()
    live = _liveness(tick)
    live.packet_received()

    snap = live.snapshot()
    assert snap.last_packet_at == datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    assert snap.last_packet_age_s == 0.0


def test_packet_age_grows_with_the_monotonic_clock():
    tick = Ticker()
    live = _liveness(tick)
    live.packet_received()

    tick.advance(4.5)

    assert live.snapshot().last_packet_age_s == 4.5


def test_a_wall_clock_step_cannot_produce_an_absurd_age():
    """NTP corrections, timezone fixes and waking from sleep all move the
    wall clock. Ages come from the monotonic stamp so a healthy device never
    reports a negative age."""
    tick = Ticker()
    wall_times = iter([
        datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc),
        datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc),  # clock jumps back
    ])
    live = _liveness(tick, wall=lambda: next(wall_times))
    live.packet_received()
    tick.advance(2.0)

    assert live.snapshot().last_packet_age_s == 2.0


def test_packets_and_transcripts_are_tracked_separately():
    """This pair is what distinguishes "alive in a quiet room" from "gone":
    gap-marker packets keep arriving while VAD suppresses silence, so packet
    flow is a heartbeat and transcript flow is speech."""
    tick = Ticker()
    live = _liveness(tick)
    live.transcript_emitted()
    tick.advance(60.0)
    live.packet_received()

    snap = live.snapshot()
    assert snap.last_packet_age_s == 0.0
    assert snap.last_transcript_age_s == 60.0
