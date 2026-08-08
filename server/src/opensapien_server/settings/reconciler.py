"""Converge the device on the settings the user chose (spec §4.2, D2).

A command is only delivered to a *live* gateway session. A wearable is
offline most of the time, so a toggle implemented as "send a command when
tapped" is dead exactly when it matters: flip it while the device is in a bag,
and nothing ever happens — but the UI says it did.

So ``/settings`` stores **desired state**, and this reconciler closes the loop.
It runs on every ``hello`` (the device just became reachable) and on every
``PUT`` that changes a device-level setting (the desired state just moved).

Firmware supports this cleanly: ``start_audio`` maps to
``audio_gate_set(false)`` and ``stop_audio`` to ``audio_gate_set(true)``, and
audio is always-on at boot — so ``start_audio`` is really *resume*, and both
commands are idempotent at the device.

**The mismatch guard is load-bearing.** ``CommandDispatcher`` dedups on
``idempotency_key`` only while a command is *unacked*. Once acked, the same
key issues a fresh command. So a reconciler that fired on every reconnect
without comparing against last-known state would issue a duplicate
``start_audio`` on every BLE flap — a command storm proportional to link
quality, worst exactly when the link is worst.
"""
from __future__ import annotations

import logging

from ..commands.issue import validate_and_issue
from .store import SettingsStore

log = logging.getLogger(__name__)

# Key under which the device's last-known audio gate lives in device state.
AUDIO_GATE = "audio_enabled"


class DeviceReconciler:
    """Issues the minimum commands needed to match desired state.

    ``dispatcher``/``ids``/``clock`` are the same ones the agent uses, so a
    reconciler-issued command is indistinguishable from any other — same
    signing, same idempotency, same audit trail.
    """

    def __init__(
        self,
        *,
        settings: SettingsStore,
        dispatcher=None,
        ids=None,
        clock=None,
        capability_provider=None,
    ) -> None:
        self._settings = settings
        self._dispatcher = dispatcher
        self._ids = ids
        self._clock = clock
        self._caps = capability_provider

    def reconcile(self) -> str | None:
        """Issue a command if the device is out of sync. Returns its type, or None.

        Returns ``None`` both when nothing needs doing and when the command
        could not be issued — the caller is the gateway's ``hello`` path and
        the settings write path, neither of which can act on a failure. The
        next reconcile retries, which is the right recovery for a device that
        is intermittently reachable anyway.
        """
        if self._dispatcher is None or self._ids is None or self._clock is None:
            return None
        desired = self._settings.get().capture.audio_enabled
        known = self._settings.get_device_state().get(AUDIO_GATE)
        if known is not None and bool(known) == desired:
            return None  # already converged — issuing here would be the storm

        command_type = "start_audio" if desired else "stop_audio"
        # The key encodes the *target state*, not the attempt. Two reconciles
        # racing toward the same state dedupe into one command; a reconcile
        # toward the opposite state does not.
        result = validate_and_issue(
            command_type=command_type,
            params={},
            idempotency_key=f"reconcile:audio:{'on' if desired else 'off'}",
            dispatcher=self._dispatcher,
            ids=self._ids,
            clock=self._clock,
            capability_provider=self._caps,
        )
        if not result.ok:
            log.warning(
                "reconcile_failed type=%s stage=%s reason=%s",
                command_type, result.stage, result.message,
            )
            return None
        log.info("reconcile_issued type=%s desired=%s known=%s",
                 command_type, desired, known)
        return command_type

    def note_acked(self, command_type: str) -> None:
        """Record that the device acked a command, updating last-known state.

        The ack is the only evidence the device actually moved. Recording
        desired state at *issue* time instead would mark an unreachable
        device as converged and stop the reconciler from ever retrying.
        """
        if command_type not in ("start_audio", "stop_audio"):
            return
        state = self._settings.get_device_state()
        state[AUDIO_GATE] = command_type == "start_audio"
        self._settings.put_device_state(state)
