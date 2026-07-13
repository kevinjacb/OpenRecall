"""Command guardrails: capability + resource + confidence checks (M11 / §6.2).

The :class:`CommandGuardrails` runs after the :class:`CommandValidator`
has schema-validated the command. It asks three questions:

  1. Does the device ADVERTISE the capability for this command type?
     (e.g. capture_photo needs camera; record_video needs camera;
      start_audio needs microphone; etc.)
  2. Does the device HAVE the resources to execute right now?
     (battery, free storage, camera-in-use, relay-connected.)
  3. Is the agent's confidence high enough to issue autonomously?

A failure becomes a :class:`RejectionReason` the audit log carries
verbatim; the Android command lifecycle UI shows the user-facing
message.

This module is a separate file from :mod:`guardrails` (the answer
guardrails) because the answer path is simpler (confidence + rate
limit) and the command path has different concerns (capability +
resource + idempotency). The :class:`StrictCommandGuardrails` here
is the binding implementation; the Protocol is the seam for tests.
"""
from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from ..contracts.types import (
    CapabilitySet,
    CapabilitySet,
    DeviceResourceStatus,
)
from .validator_command import ValidatedCommand


# Resource estimates for the storage-bounded commands. The constants
# here are conservative — a real device may compress better, but the
# guardrails should err on the side of refusing rather than risking
# a write failure mid-recording. Tweak with real-device data once
# the INMP144s land.
_STORAGE_BYTES_PER_SECOND = 1 << 20  # 1 MB/s — placeholder; tune per-device
_BATTERY_MIN_FOR_LONG_OP = 0.10       # 10% — refuse below
_BATTERY_MIN_FOR_QUICK_OP = 0.05      # 5% — refuse below for capture_photo / stop_audio


class RejectionReason(str, Enum):
    """Why the command guardrails refused a command.

    Stable wire strings. The Android UI maps each value to a
    user-facing message. Adding a new value is a contract change.
    """

    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    RESOURCE_UNAVAILABLE = "resource_unavailable"
    CONFIDENCE_TOO_LOW = "command_confidence_too_low"


# Capability requirements per command type. Each entry is a callable
# that takes a CapabilitySet and returns True if the device advertises
# what's needed. Centralized here so adding a new command type
# requires touching exactly one place.
def _needs_camera(caps: CapabilitySet) -> bool:
    return caps.camera

def _needs_microphone(caps: CapabilitySet) -> bool:
    return caps.microphone

def _needs_retrospective_buffer(caps: CapabilitySet) -> bool:
    return caps.retrospective_buffer


def _capability_name(command_type: str) -> str:
    """Human-readable capability name for an error message."""
    return {
        "capture_photo": "camera",
        "record_video": "camera",
        "start_audio": "microphone",
        "stop_audio": "microphone",
        "request_buffer": "retrospective_buffer",
    }.get(command_type, "unknown")


CAPABILITY_REQUIREMENTS: dict[str, "callable"] = {
    "capture_photo": _needs_camera,
    "record_video": _needs_camera,
    "start_audio": _needs_microphone,
    "stop_audio": _needs_microphone,  # also needs the mic
    "request_buffer": _needs_retrospective_buffer,
}


@runtime_checkable
class CommandGuardrails(Protocol):
    """Single seam for "is this command safe + allowed right now?"."""

    def check(self, command: ValidatedCommand) -> "CommandGuardrailsResult": ...


class CommandGuardrailsResult(BaseModel):
    """The output of :class:`CommandGuardrails`.

    Either ``allowed is True`` (and ``command`` is the validated
    command, ready to dispatch) or ``allowed is False`` (and
    ``rejection`` + ``message`` explain why). A non-None ``rejection``
    is the contract for refusal; the audit log carries it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    allowed: bool
    command: ValidatedCommand | None = None
    rejection: RejectionReason | None = None
    message: str | None = None


class StrictCommandGuardrails:
    """Default :class:`CommandGuardrails` implementation.

    Stateless; safe to share across requests. Every consumer
    (Planner, HTTP routes) reads the same instance.

    The constructor takes a :class:`CapabilityProvider` (so the
    guardrails see what the device advertises) plus a confidence
    threshold. The resource read happens at check-time, so the
    guardrails always see the current battery / storage / camera-in-use
    snapshot.
    """

    def __init__(
        self,
        capabilities: CapabilitySet | None = None,
        resources: DeviceResourceStatus | None = None,
        confidence_autonomous: float = 0.85,
    ) -> None:
        # The guardrails take explicit capability / resource objects
        # rather than a :class:`CapabilityProvider` so they're easy
        # to test (pass a static CapabilitySet, no provider indirection).
        # The Planner is responsible for snapshotting the provider at
        # check-time and passing the current values in.
        self._capabilities = capabilities or CapabilitySet()
        self._resources = resources or DeviceResourceStatus()
        self._autonomous = confidence_autonomous

    def check(self, command: ValidatedCommand) -> CommandGuardrailsResult:
        # 1. Capability check (most specific: the firmware doesn't
        # know how to execute this).
        requires = CAPABILITY_REQUIREMENTS.get(command.command_type)
        if requires is not None and not requires(self._capabilities):
            return CommandGuardrailsResult(
                allowed=False,
                rejection=RejectionReason.CAPABILITY_UNAVAILABLE,
                message=(
                    f"device does not advertise the capability required for "
                    f"{command.command_type!r}: missing {_capability_name(command.command_type)}"
                ),
            )

        # 2. Resource check (observable: low battery, no storage,
        # camera in use, relay disconnected).
        resource_msg = self._resource_issue(command)
        if resource_msg is not None:
            return CommandGuardrailsResult(
                allowed=False,
                rejection=RejectionReason.RESOURCE_UNAVAILABLE,
                message=resource_msg,
            )

        # 3. Confidence gate (autonomous threshold; commands below
        # this are refused for now — a future "request confirmation"
        # path can be added without changing the seam).
        if command.confidence < self._autonomous:
            return CommandGuardrailsResult(
                allowed=False,
                rejection=RejectionReason.CONFIDENCE_TOO_LOW,
                message=(
                    f"confidence {command.confidence} is below the "
                    f"autonomous threshold {self._autonomous}"
                ),
            )

        return CommandGuardrailsResult(
            allowed=True,
            command=command,
            rejection=None,
            message=None,
        )

    def _resource_issue(self, command: ValidatedCommand) -> str | None:
        """Return a user-facing message if a resource check fails, else None."""
        # Relay connectivity is a hard prerequisite.
        if not self._resources.relay_connected:
            return "device is not connected to the relay"
        # Battery threshold depends on the operation's duration.
        # Long operations (record_video with high duration_s, anything
        # with 10s+ in the params) need a higher battery floor.
        min_battery = self._min_battery_for(command)
        if self._resources.battery_pct < min_battery:
            return (
                f"battery {self._resources.battery_pct:.0%} is below the "
                f"{min_battery:.0%} minimum for {command.command_type!r}"
            )
        # Storage: only record_video / request_buffer actually
        # write audio; check the duration-scaled estimate.
        if command.command_type in ("record_video", "request_buffer"):
            seconds = command.params.get("duration_s") or command.params.get("seconds") or 0
            needed = seconds * _STORAGE_BYTES_PER_SECOND
            if self._resources.storage_free_bytes < needed:
                return (
                    f"only {self._resources.storage_free_bytes} bytes free; "
                    f"{command.command_type!r} needs {needed} bytes"
                )
        # Camera in use: only blocks capture_photo (record_video
        # is also in use on the camera but a long-running video
        # doesn't prevent a new photo — well, it does, but the
        # firmware handles the queue. For now, refuse on
        # capture_photo only; record_video handles its own backpressure).
        if command.command_type == "capture_photo" and self._resources.recording:
            return "camera is currently recording"
        return None

    def _min_battery_for(self, command: ValidatedCommand) -> float:
        """Long-running operations need more battery headroom than quick ones."""
        long_ops = ("record_video", "request_buffer")
        if command.command_type in long_ops:
            seconds = command.params.get("duration_s") or command.params.get("seconds") or 0
            if seconds > 10:
                return _BATTERY_MIN_FOR_LONG_OP
        return _BATTERY_MIN_FOR_QUICK_OP
