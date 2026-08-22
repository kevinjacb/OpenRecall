"""Tests for CapabilityProvider."""
from __future__ import annotations

from openrecall_server.agent.capability import ConstantCapabilityProvider
from openrecall_server.contracts.types import (
    CapabilityProvider,
    CapabilitySet,
    DeviceResourceStatus,
)


def test_constant_provider_returns_default_caps():
    p = ConstantCapabilityProvider()
    caps = p.capabilities()
    assert isinstance(caps, CapabilitySet)
    assert caps.microphone is True
    assert caps.camera is False


def test_constant_provider_custom_caps():
    caps = CapabilitySet(camera=True, microphone=True)
    p = ConstantCapabilityProvider(capabilities=caps)
    assert p.capabilities().camera is True


def test_constant_provider_returns_default_resources():
    p = ConstantCapabilityProvider()
    r = p.resources()
    assert isinstance(r, DeviceResourceStatus)
    assert r.battery_pct == 1.0


def test_provider_satisfies_protocol():
    from openrecall_server.contracts.types import CapabilityProvider
    assert isinstance(ConstantCapabilityProvider(), CapabilityProvider)


# --- ReportedCapabilityProvider (P2, telemetry-backed) ---

from openrecall_server.agent.capability import ReportedCapabilityProvider


def test_reported_provider_defaults_before_any_telemetry():
    p = ReportedCapabilityProvider()
    assert p.resources().battery_pct == 1.0  # safe default; guardrails clear all floors
    assert p.state() == "unknown"
    assert p.last_wake_reason() is None
    # other DeviceResourceStatus defaults stay sensible
    r = p.resources()
    assert r.relay_connected is True
    assert r.recording is False


def test_reported_provider_report_updates_battery_and_state():
    p = ReportedCapabilityProvider()
    p.report(battery_pct=0.42, state="active", wake_reason="button")
    assert p.resources().battery_pct == 0.42
    assert p.state() == "active"
    assert p.last_wake_reason() == "button"


def test_reported_provider_report_is_thread_safe_under_concurrent_writes():
    import threading
    p = ReportedCapabilityProvider()

    def write():
        for i in range(200):
            p.report(battery_pct=(i % 100) / 100, state="active", wake_reason=None)

    ts = [threading.Thread(target=write) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    # no exception + a valid in-range value
    assert 0.0 <= p.resources().battery_pct <= 1.0


def test_reported_provider_satisfies_protocol():
    from openrecall_server.contracts.types import CapabilityProvider
    assert isinstance(ReportedCapabilityProvider(), CapabilityProvider)


def test_reported_provider_custom_capabilities():
    caps = CapabilitySet(camera=True, microphone=True)
    p = ReportedCapabilityProvider(capabilities=caps)
    assert p.capabilities().camera is True
    assert p.capabilities().microphone is True
