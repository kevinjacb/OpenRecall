"""CapabilityProvider — the single seam for "what can the device do?".

The P2-answers slice uses a constant stub because the device capability
advertisement (a BLE characteristic) isn't wired in yet. The Protocol
and the stub give a stable seam so P3-commands can drop in a real
``DeviceCharacteristicProvider`` without changing any consumer.
"""
from __future__ import annotations

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
