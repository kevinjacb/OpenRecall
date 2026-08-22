"""Tests for the command guardrails: capability + resource checks.

The :class:`CommandGuardrails` is the second line of defense after the
:class:`CommandValidator`. It runs after the command has been
schema-validated and asks:

  1. Does the device ADVERTISE the capability for this command type?
     (e.g. capture_photo needs camera; record_video needs camera;
      start_audio needs microphone; etc.)
  2. Does the device HAVE the resources to execute right now?
     (e.g. record_video needs enough storage; capture_photo needs
      the camera not currently in use.)
  3. Is the agent's confidence high enough to issue autonomously?

Failures become :class:`RejectionReason` strings the audit log
carries verbatim. The Android command lifecycle UI shows the
user-facing message.
"""
from __future__ import annotations

import pytest

from openrecall_server.contracts.types import (
    CapabilitySet,
    DeviceResourceStatus,
)
from openrecall_server.agent.validator_command import ValidatedCommand
from openrecall_server.agent.guardrails_command import (
    CommandGuardrails,
    CommandGuardrailsResult,
    RejectionReason,
    StrictCommandGuardrails,
)


def _command(
    command_type: str = "capture_photo",
    params: dict | None = None,
    confidence: float = 0.9,
    idempotency_key: str = "ik-1",
) -> ValidatedCommand:
    return ValidatedCommand(
        command_type=command_type,  # type: ignore[arg-type]
        params=params or {},
        idempotency_key=idempotency_key,
        confidence=confidence,
    )


def _caps(
    camera: bool = True,
    microphone: bool = True,
    retrospective_buffer: bool = True,
) -> CapabilitySet:
    return CapabilitySet(
        camera=camera,
        microphone=microphone,
        retrospective_buffer=retrospective_buffer,
    )


def _resources(
    battery_pct: float = 1.0,
    storage_free_bytes: int = 1 << 30,
    camera_available: bool = True,
    microphone_available: bool = True,
    recording: bool = False,
) -> DeviceResourceStatus:
    return DeviceResourceStatus(
        battery_pct=battery_pct,
        storage_free_bytes=storage_free_bytes,
        camera_available=camera_available,
        microphone_available=microphone_available,
        recording=recording,
        relay_connected=True,
    )


# --- protocol shape ----------------------------------------------------------


def test_strict_command_guardrails_satisfies_protocol():
    g = StrictCommandGuardrails(
        _caps(), _resources(), confidence_autonomous=0.85
    )
    assert isinstance(g, CommandGuardrails)


# --- capability checks ------------------------------------------------------


def test_capture_photo_requires_camera():
    """If the device doesn't advertise a camera, the command is refused."""
    g = StrictCommandGuardrails(_caps(camera=False), _resources())
    out = g.check(_command("capture_photo"))
    assert out.rejection == RejectionReason.CAPABILITY_UNAVAILABLE
    assert "camera" in (out.message or "")


def test_record_video_requires_camera():
    g = StrictCommandGuardrails(_caps(camera=False), _resources())
    out = g.check(_command("record_video", {"duration_s": 10}))
    assert out.rejection == RejectionReason.CAPABILITY_UNAVAILABLE


def test_start_audio_requires_microphone():
    g = StrictCommandGuardrails(_caps(microphone=False), _resources())
    out = g.check(_command("start_audio"))
    assert out.rejection == RejectionReason.CAPABILITY_UNAVAILABLE


def test_request_buffer_requires_retrospective_buffer():
    """The retrospective_buffer capability is the device's ability to
    re-emit past audio. Without it, request_buffer is meaningless."""
    g = StrictCommandGuardrails(
        _caps(retrospective_buffer=False), _resources()
    )
    out = g.check(_command("request_buffer", {"seconds": 30}))
    assert out.rejection == RejectionReason.CAPABILITY_UNAVAILABLE


# --- resource checks --------------------------------------------------------


def test_capture_photo_requires_camera_available():
    """Camera is in use (recording=True), so capture_photo is refused."""
    g = StrictCommandGuardrails(_caps(), _resources(recording=True))
    out = g.check(_command("capture_photo"))
    assert out.rejection == RejectionReason.RESOURCE_UNAVAILABLE


def test_record_video_requires_sufficient_storage():
    """At 1MB/s for 30s = 30MB. With only 1MB free, refuse."""
    g = StrictCommandGuardrails(_caps(), _resources(storage_free_bytes=1 << 20))
    out = g.check(_command("record_video", {"duration_s": 30}))
    assert out.rejection == RejectionReason.RESOURCE_UNAVAILABLE


def test_record_video_short_duration_uses_less_storage():
    """A 1s video uses ~1MB. With 1MB free, that's borderline but should
    pass. This validates the resource estimate is duration-aware, not
    a constant overhead."""
    g = StrictCommandGuardrails(_caps(), _resources(storage_free_bytes=1 << 20))
    out = g.check(_command("record_video", {"duration_s": 1}))
    assert out.rejection is None


def test_low_battery_refuses_command():
    """Below 5% battery, the device can't safely run a long operation."""
    g = StrictCommandGuardrails(_caps(), _resources(battery_pct=0.04))
    out = g.check(_command("record_video", {"duration_s": 10}))
    assert out.rejection == RejectionReason.RESOURCE_UNAVAILABLE
    assert "battery" in (out.message or "").lower()


def test_low_battery_refuses_a_long_op_on_the_long_op_floor():
    """A long op (record_video >10s, floor 0.10) is refused on a battery that
    clears the quick-op floor (0.05) but not the long-op floor (0.10) — pinning
    that the floor reads resources().battery_pct and that the >10s branch of
    _min_battery_for applies. T9 wires ReportedCapabilityProvider.resources()
    into here, so this pin trusts that wiring."""
    g = StrictCommandGuardrails(_caps(), _resources(battery_pct=0.07))
    out = g.check(_command("record_video", {"duration_s": 30}))
    assert not out.allowed
    assert "battery" in (out.message or "").lower()


def test_disconnected_relay_refuses_command():
    g = StrictCommandGuardrails(
        _caps(),
        DeviceResourceStatus(relay_connected=False),
    )
    out = g.check(_command("capture_photo"))
    assert out.rejection == RejectionReason.RESOURCE_UNAVAILABLE


# --- confidence gate ---------------------------------------------------------


def test_high_confidence_command_passes():
    g = StrictCommandGuardrails(_caps(), _resources(), confidence_autonomous=0.85)
    out = g.check(_command(confidence=0.95))
    assert out.rejection is None
    assert out.allowed is True


def test_low_confidence_command_is_refused():
    g = StrictCommandGuardrails(_caps(), _resources(), confidence_autonomous=0.85)
    out = g.check(_command(confidence=0.5))
    assert out.rejection == RejectionReason.CONFIDENCE_TOO_LOW


def test_at_confidence_threshold_command_passes():
    g = StrictCommandGuardrails(_caps(), _resources(), confidence_autonomous=0.85)
    out = g.check(_command(confidence=0.85))
    assert out.rejection is None


# --- ordering: capability before resource before confidence ---------------


def test_capability_failure_takes_precedence_over_resource_failure():
    """If the device lacks BOTH the capability AND the resource,
    the capability check fails first (more specific, easier to fix)."""
    g = StrictCommandGuardrails(
        _caps(camera=False),
        _resources(recording=True),
    )
    out = g.check(_command("capture_photo"))
    assert out.rejection == RejectionReason.CAPABILITY_UNAVAILABLE


def test_resource_failure_takes_precedence_over_confidence_failure():
    g = StrictCommandGuardrails(
        _caps(),
        _resources(battery_pct=0.04),
        confidence_autonomous=0.99,
    )
    out = g.check(_command(confidence=0.5))
    assert out.rejection == RejectionReason.RESOURCE_UNAVAILABLE


# --- result contract ---------------------------------------------------------


def test_allowed_result_marks_command_as_approved():
    g = StrictCommandGuardrails(_caps(), _resources(), confidence_autonomous=0.85)
    out = g.check(_command(confidence=0.95))
    assert out.allowed is True
    assert out.rejection is None
    assert out.command is not None
    assert out.message is None


def test_refused_result_marks_command_as_not_allowed():
    g = StrictCommandGuardrails(_caps(), _resources(), confidence_autonomous=0.85)
    out = g.check(_command(confidence=0.3))
    assert out.allowed is False
    assert out.rejection is not None
    assert out.message is not None
    assert out.command is None


# --- P1 instruction processor: record_audio + start/stop_video + flush_snapshots


def test_record_audio_needs_microphone():
    g = StrictCommandGuardrails(
        _caps(microphone=False), _resources()
    )
    out = g.check(_command("record_audio", {"duration_s": 20}))
    assert not out.allowed
    assert out.rejection == RejectionReason.CAPABILITY_UNAVAILABLE
    assert "microphone" in (out.message or "")


def test_record_audio_passes_with_mic_and_storage():
    g = StrictCommandGuardrails(_caps(), _resources())
    assert g.check(_command("record_audio", {"duration_s": 20})).allowed


def test_record_audio_refused_on_low_storage():
    g = StrictCommandGuardrails(
        _caps(), _resources(storage_free_bytes=0)
    )
    out = g.check(_command("record_audio", {"duration_s": 20}))
    assert not out.allowed
    assert out.rejection == RejectionReason.RESOURCE_UNAVAILABLE


def test_start_video_needs_camera():
    g = StrictCommandGuardrails(
        _caps(camera=False), _resources()
    )
    for t in ("start_video", "stop_video", "flush_snapshots"):
        out = g.check(_command(t))
        assert not out.allowed, t
        assert out.rejection == RejectionReason.CAPABILITY_UNAVAILABLE, t
        assert "camera" in (out.message or ""), t


def test_start_video_passes_with_camera():
    g = StrictCommandGuardrails(_caps(), _resources())
    for t in ("start_video", "stop_video", "flush_snapshots"):
        assert g.check(_command(t)).allowed, t


# --- P2 power/sleep: sleep must be issuable on a critically low battery --------


def test_sleep_is_issuable_on_critically_low_battery():
    """Sleep is the path TO low power — it must be issuable even when the
    battery is below the 0.05 quick-op floor that previously refused it
    (spec §2.1; plan T1 note). 0.02 is below that floor, so this isolates
    the _min_battery_for early-return for sleep."""
    g = StrictCommandGuardrails(_caps(), _resources(battery_pct=0.02))
    out = g.check(_command("sleep"))
    assert out.allowed is True


# --- P3 video: set_snapshot_interval has no battery floor ---------------------


def test_set_snapshot_interval_issuable_on_low_battery():
    """set_snapshot_interval is a quick config change, not an energy op — it
    must be issuable even on a critically low battery (like sleep). 0.02 is
    below the 0.05 quick-op floor, so this isolates the _min_battery_for
    early-return for set_snapshot_interval."""
    g = StrictCommandGuardrails(_caps(), _resources(battery_pct=0.02))
    out = g.check(_command("set_snapshot_interval", {"seconds": 120}))
    assert out.allowed is True


def test_set_snapshot_interval_has_no_capability_requirement():
    """set_snapshot_interval needs no device capability (like sleep) — a
    device with no camera/mic/buffer must still accept the cadence config."""
    g = StrictCommandGuardrails(
        _caps(camera=False, microphone=False, retrospective_buffer=False),
        _resources(),
    )
    out = g.check(_command("set_snapshot_interval", {"seconds": 60}))
    assert out.allowed is True
