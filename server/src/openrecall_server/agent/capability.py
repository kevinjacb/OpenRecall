"""CapabilityProvider — the single seam for "what can the device do?".

The P2-answers slice uses a constant stub because the device capability
advertisement (a BLE characteristic) isn't wired in yet. The Protocol
and the stub give a stable seam so P3-commands can drop in a real
``DeviceCharacteristicProvider`` without changing any consumer.
"""
from __future__ import annotations

import threading
from typing import Callable

from ..contracts.types import (
    CapabilityProvider,
    CapabilitySet,
    DeviceResourceStatus,
)


class ConstantCapabilityProvider:
    """A capability provider that returns the same :class:`CapabilitySet`
    and :class:`DeviceResourceStatus` for every call.

    Used in the gateway default wiring. The P3-commands slice replaces
    this with a real BLE-characteristic-backed provider.
    """

    def __init__(
        self,
        capabilities: CapabilitySet | None = None,
        resources: DeviceResourceStatus | None = None,
    ) -> None:
        self._capabilities = capabilities or CapabilitySet(
            camera=False,
            microphone=True,
            retrospective_buffer=True,
            display=False,
            speaker=False,
        )
        self._resources = resources or DeviceResourceStatus()

    def capabilities(self) -> CapabilitySet:
        return self._capabilities

    def resources(self) -> DeviceResourceStatus:
        return self._resources


class ReportedCapabilityProvider:
    """CapabilityProvider backed by device telemetry (P2 power/sleep).

    Replaces ``ConstantCapabilityProvider`` in the live wiring: the
    gateway's ``on_telemetry`` calls :meth:`report` on each Telemetry
    frame; routes and the command guardrails read :meth:`resources` for
    the real ``battery_pct``. Defaults (``battery=1.0``, ``state='unknown'``)
    are safe — guardrails clear all floors until the first telemetry
    lands, so a freshly-booted device with no telemetry yet is not
    refused.

    Thread-safe: telemetry arrives on the gateway thread; routes and
    guardrails read on the request/agent threads. A ``threading.Lock``
    guards the reported-state read/write (matches the project's
    SQLite-store thread-safety pattern).
    """

    def __init__(
        self,
        capabilities: CapabilitySet | None = None,
        vision_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self._capabilities = capabilities or CapabilitySet(
            camera=False,
            microphone=True,
            retrospective_buffer=True,
            display=False,
            speaker=False,
        )
        self._vision_enabled = vision_enabled  # None → follow base capabilities
        self._lock = threading.Lock()
        self._battery_pct: float = 1.0
        self._state: str = "unknown"
        self._wake_reason: str | None = None

    def report(
        self,
        *,
        battery_pct: float,
        state: str,
        wake_reason: str | None,
    ) -> None:
        """Update the reported device resource snapshot.

        Called from the gateway thread on each Telemetry frame. All three
        fields are written together under the lock so a concurrent reader
        never sees a torn snapshot.
        """
        with self._lock:
            self._battery_pct = battery_pct
            self._state = state
            self._wake_reason = wake_reason

    def state(self) -> str:
        """Latest reported device power state (e.g. ``"active"``,
        ``"sleep"``). ``"unknown"`` until the first telemetry lands."""
        with self._lock:
            return self._state

    def last_wake_reason(self) -> str | None:
        """Latest reported wake reason, or ``None`` if none has been
        reported or the device has not woken since last reset."""
        with self._lock:
            return self._wake_reason

    def capabilities(self) -> CapabilitySet:
        caps = self._capabilities
        if self._vision_enabled is None:
            return caps
        # P3 §3.3: when the vision_enabled seam is present, camera availability
        # FOLLOWS the toggle (override, not AND). The live wiring constructs this
        # provider with the default base (camera=False) and vision_enabled as the
        # camera-available signal — ANDing would keep camera False forever. One
        # seam flips /device/status.camera_available AND gates the camera
        # commands via the existing _needs_camera guardrail (which reads
        # capabilities().camera) — no guardrails signature change.
        on = bool(self._vision_enabled())
        if caps.camera == on:
            return caps
        return caps.model_copy(update={"camera": on})

    def resources(self) -> DeviceResourceStatus:
        with self._lock:
            battery = self._battery_pct
        return DeviceResourceStatus(battery_pct=battery)
