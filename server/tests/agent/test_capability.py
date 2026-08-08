"""Tests for CapabilityProvider."""
from __future__ import annotations

from opensapien_server.agent.capability import ConstantCapabilityProvider
from opensapien_server.contracts.types import (
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
    from opensapien_server.contracts.types import CapabilityProvider
    assert isinstance(ConstantCapabilityProvider(), CapabilityProvider)
