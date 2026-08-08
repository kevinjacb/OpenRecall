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

from opensapien_server.contracts.types import (
    CapabilitySet,
    DeviceResourceStatus,
)
from opensapien_server.agent.validator_command import ValidatedCommand
from opensapien_server.agent.guardrails_command import (
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
