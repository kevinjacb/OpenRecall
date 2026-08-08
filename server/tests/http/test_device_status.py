"""Phase 5 HTTP — GET /device/status (spec §5.1, D9)."""
from __future__ import annotations

from datetime import datetime, timezone

from aiohttp.test_utils import TestClient, TestServer

from opensapien_server.agent.capability import ConstantCapabilityProvider
from opensapien_server.contracts.types import CapabilitySet
from opensapien_server.gateway.liveness import DeviceLiveness
from opensapien_server.http.app import build_app
from opensapien_server.sessions.lifecycle import SessionLifecycle

_AUTH = {"Authorization": "Bearer t"}
_WALL = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


class Ticker:
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _client(*, liveness=None, lifecycle=None, capabilities=None):
    app = build_app(
        token="t",
        get_pubkey=lambda: b"\x00" * 32,
        liveness=liveness,
        session_lifecycle=lifecycle,
        capability_provider=ConstantCapabilityProvider(capabilities=capabilities),
    )
    return TestClient(TestServer(app))


async def _get(**kwargs):
    async with _client(**kwargs) as client:
        resp = await client.get("/device/status", headers=_AUTH)
        return resp.status, await resp.json()


# ---- the placeholder (D9) ---------------------------------------------------


async def test_battery_ships_as_a_flagged_placeholder():
    """`source: "static"` is how a client knows the number is not measured.
    A plausible-looking 45% is worse than a blank if anyone acts on it."""
    status, body = await _get()

    assert status == 200
    assert body["battery_pct"] == 0.45
    assert body["source"] == "static"


async def test_unmeasured_telemetry_is_null_not_invented():
    status, body = await _get()

    assert body["storage_free_bytes"] is None
    assert body["firmware_version"] is None


async def test_the_placeholder_does_not_leak_into_command_admission():
    """The 45% lives on the presentation surface only. The guardrails read
    `ConstantCapabilityProvider`, whose battery_pct stays 1.0 — coupling a
    display placeholder to command admission would mean a future placeholder
    below 10% silently starts refusing request_buffer."""
    assert ConstantCapabilityProvider().resources().battery_pct == 1.0


# ---- what is genuinely measured ---------------------------------------------


async def test_relay_connected_reflects_a_live_connection():
    liveness = DeviceLiveness(monotonic=Ticker(), wall=lambda: _WALL)
    liveness.connection_opened()

    status, body = await _get(liveness=liveness)

    assert body["relay_connected"] is True


async def test_relay_connected_is_false_after_a_disconnect():
    liveness = DeviceLiveness(monotonic=Ticker(), wall=lambda: _WALL)
    liveness.connection_opened()
    liveness.connection_closed()

    status, body = await _get(liveness=liveness)

    assert body["relay_connected"] is False


async def test_recording_reflects_the_lifecycle_registry():
    lifecycle = SessionLifecycle()
    lifecycle.register("s1")

    status, body = await _get(lifecycle=lifecycle)

    assert body["recording"] is True


async def test_recording_is_false_once_the_session_deregisters():
    """This depends on the Phase 0.1 fix — without it a dropped session
    reported as still recording forever, which is exactly the situation this
    field exists to catch."""
    lifecycle = SessionLifecycle()
    lifecycle.register("s1")
    lifecycle.deregister("s1")

    status, body = await _get(lifecycle=lifecycle)

    assert body["recording"] is False


async def test_packet_freshness_is_reported():
    tick = Ticker()
    liveness = DeviceLiveness(monotonic=tick, wall=lambda: _WALL)
    liveness.packet_received()
    tick.advance(4.0)

    status, body = await _get(liveness=liveness)

    assert body["last_packet_at"] == _WALL.isoformat()
    assert body["last_packet_age_s"] == 4.0


async def test_packet_and_transcript_freshness_are_distinct():
    """Alive in a quiet room versus gone: gap-marker packets keep arriving
    while VAD suppresses silence, so a fresh packet with a stale transcript
    is a healthy device in a quiet room."""
    tick = Ticker()
    liveness = DeviceLiveness(monotonic=tick, wall=lambda: _WALL)
    liveness.transcript_emitted()
    tick.advance(300.0)
    liveness.packet_received()

    status, body = await _get(liveness=liveness)

    assert body["last_packet_age_s"] == 0.0
    assert body["last_transcript_age_s"] == 300.0


async def test_a_never_connected_device_reports_nulls_not_zeros():
    status, body = await _get()

    assert body["relay_connected"] is False
    assert body["last_packet_at"] is None
    assert body["last_packet_age_s"] is None


async def test_capabilities_come_from_the_provider():
    status, body = await _get(capabilities=CapabilitySet(microphone=True, camera=False))

    assert body["microphone_available"] is True
    assert body["camera_available"] is False


# ---- robustness -------------------------------------------------------------


async def test_the_endpoint_never_fails_even_with_no_dependencies():
    """Same rule as /status: an endpoint that reports health must not itself
    be a thing that fails."""
    status, body = await _get()

    assert status == 200
    assert body["schema_version"] == "v1"


async def test_a_broken_dependency_degrades_rather_than_500s():
    class Exploding:
        def snapshot(self):
            raise RuntimeError("boom")

    status, body = await _get(liveness=Exploding())

    assert status == 200
    assert body["relay_connected"] is False


async def test_device_status_requires_a_token():
    async with _client() as client:
        resp = await client.get("/device/status")

    assert resp.status == 401
