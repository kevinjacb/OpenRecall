"""GET /device/status — the Settings header (spec §5.1, D9).

This endpoint is honest about a split between what it knows and what it does
not, and that split is the design.

**What is genuinely measured** is connection freshness: whether a relay is
connected, whether a session is recording, when the last audio packet arrived,
and when the last transcript was produced. All of it comes from the gateway,
none of it needs firmware, and together the packet and transcript timestamps
separate the three states a user actually cares about — alive and hearing
speech, alive in a quiet room, and gone.

Firmware keeps emitting ``C6_GAP_MARKER`` packets while VAD suppresses
silence, so packet arrival is a true heartbeat rather than a speech detector.
The bring-up log records exactly this case: gap-marker packets flowing with no
transcripts.

**What is a placeholder** is ``battery_pct``, which ships as a fixed 45 %.
Battery sensing does not exist at any layer — no ADC channel, no fuel-gauge
driver, no ``vbat`` reference anywhere in the firmware. Making it real is
hardware bring-up, possibly a board revision, not a protocol change, so it is
parked (D9) and the placeholder exists only so the header has something to
lay out against. ``source: "static"`` is how a client knows not to trust it;
it should be rendered visibly provisional. A plausible-looking 45 % is worse
than a blank if anyone ever acts on it.
"""
from __future__ import annotations

from aiohttp import web

# The placeholder lives **here**, on the presentation surface, and not in
# `ConstantCapabilityProvider`. The provider's `battery_pct` is what
# `StrictCommandGuardrails` reads for its refuse-below floors
# (_BATTERY_MIN_FOR_LONG_OP = 0.10, _BATTERY_MIN_FOR_QUICK_OP = 0.05). 45 %
# clears both, so moving it would be harmless *today* — but it would couple a
# display placeholder to command admission, and a future placeholder below
# 10 % would silently start refusing `request_buffer`.
_PLACEHOLDER_BATTERY_PCT = 0.45


def add_routes(app: web.Application) -> None:
    app.router.add_get("/device/status", get_device_status)


def _safe(fn, default=None):
    """Call ``fn``, falling back to ``default`` on any failure.

    Same rule as ``/status``: this endpoint reports health, so it must not
    itself be a thing that fails. A partial answer at 200 is more useful than
    a 500 that tells the user nothing about their device.
    """
    try:
        return fn()
    except Exception:
        return default


async def get_device_status(request: web.Request) -> web.Response:
    app = request.app
    liveness = app.get("sense_liveness")
    lifecycle = app.get("sense_session_lifecycle")
    provider = app.get("sense_capability_provider")

    snapshot = _safe(liveness.snapshot) if liveness is not None else None
    capabilities = _safe(provider.capabilities) if provider is not None else None

    recording = _safe(lambda: len(lifecycle) > 0, False) if lifecycle is not None else False

    return web.json_response({
        "schema_version": "v1",
        # "static" means the device-reported numbers are not measured. When
        # real telemetry lands this becomes "device" and the placeholder is
        # deleted rather than kept as a fallback.
        "source": "static",
        "battery_pct": _PLACEHOLDER_BATTERY_PCT,
        "storage_free_bytes": None,
        "firmware_version": None,
        # --- genuinely measured from here down ---
        "recording": bool(recording),
        "relay_connected": bool(snapshot.connected) if snapshot else False,
        "microphone_available": bool(capabilities.microphone) if capabilities else True,
        "camera_available": bool(capabilities.camera) if capabilities else False,
        "last_packet_at": (
            snapshot.last_packet_at.isoformat()
            if snapshot and snapshot.last_packet_at else None
        ),
        "last_packet_age_s": snapshot.last_packet_age_s if snapshot else None,
        "last_transcript_at": (
            snapshot.last_transcript_at.isoformat()
            if snapshot and snapshot.last_transcript_at else None
        ),
        "last_transcript_age_s": snapshot.last_transcript_age_s if snapshot else None,
    })
