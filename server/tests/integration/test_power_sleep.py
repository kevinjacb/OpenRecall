"""P2 power/sleep exit criteria (spec §2.8, sim-only).

The full server-side power path with no hardware: telemetry flows into
provider/state/settings; a button-wake clears desired sleep; desired
sleep_mode issues 'sleep'; the guardrails' battery floors read the
reported value. The device simulator stands in for the XIAU + relay.
"""
from openrecall_server.agent.capability import ReportedCapabilityProvider
from openrecall_server.agent.guardrails_command import StrictCommandGuardrails
from openrecall_server.agent.validator_command import ValidatedCommand
from openrecall_server.commands.dispatcher import CommandDispatcher
from openrecall_server.commands.signing import CommandSigner
from openrecall_server.contracts.clock import FakeClock
from openrecall_server.contracts.id_generator import DeterministicIdGenerator
from openrecall_server.contracts.types import CapabilitySet
from openrecall_server.gateway.core import GatewayCore
from openrecall_server.protocol.messages import Telemetry
from openrecall_server.settings.model import SettingsDocument
from openrecall_server.settings.reconciler import DeviceReconciler
from openrecall_server.settings.store import InMemorySettingsStore


class _NoopPipeline:
    """Pipeline stub that satisfies PipelineFactory without the audio path."""

    def ingest(self, packet):  # noqa: ANN001 - signature matches the real pipeline
        return []

    def missing_range(self):
        return None

    @property
    def next_expected_seq(self) -> int:
        return 0


def test_telemetry_flows_into_device_status():
    """A telemetry frame updates the provider and clears desired sleep on
    button-wake; the HTTP surface is covered by T7's unit tests."""
    provider = ReportedCapabilityProvider()
    settings = InMemorySettingsStore(
        SettingsDocument.model_validate({"capture": {"sleep_mode": True}}),
    )
    core = GatewayCore(
        pipeline_factory=lambda start_seq: _NoopPipeline(),
        settings=settings,
        capability_provider=provider,
    )
    core.on_control(
        Telemetry(
            session_id="s1",
            battery_pct=0.61,
            state="active",
            wake_reason="button",
        ),
    )
    assert provider.resources().battery_pct == 0.61
    assert provider.state() == "active"
    assert settings.get().capture.sleep_mode is False  # button-wake cleared it


def test_desired_sleep_mode_issues_sleep_via_reconciler():
    clock = FakeClock()
    dispatcher = CommandDispatcher(CommandSigner.generate(), clock=clock.now)
    settings = InMemorySettingsStore(
        SettingsDocument.model_validate({"capture": {"sleep_mode": True}}),
    )
    rec = DeviceReconciler(
        settings=settings,
        dispatcher=dispatcher,
        ids=DeterministicIdGenerator(),
        clock=clock,
    )
    assert rec.reconcile() == "sleep"
    assert [c.command.type for c in dispatcher.pending()] == ["sleep"]


def test_button_wake_then_reconciler_does_not_re_sleep():
    """Button-wake clears desired sleep; the reconciler then resumes audio,
    it does NOT re-issue sleep (D2)."""
    clock = FakeClock()
    dispatcher = CommandDispatcher(CommandSigner.generate(), clock=clock.now)
    settings = InMemorySettingsStore(
        SettingsDocument.model_validate(
            {"capture": {"sleep_mode": True, "audio_enabled": True}},
        ),
    )
    provider = ReportedCapabilityProvider()
    core = GatewayCore(
        pipeline_factory=lambda start_seq: _NoopPipeline(),
        settings=settings,
        capability_provider=provider,
    )
    rec = DeviceReconciler(
        settings=settings,
        dispatcher=dispatcher,
        ids=DeterministicIdGenerator(),
        clock=clock,
        capability_provider=provider,
    )
    # Button wake arrives -> clears desired sleep.
    core.on_control(
        Telemetry(
            session_id="s1",
            battery_pct=0.9,
            state="active",
            wake_reason="button",
        ),
    )
    assert settings.get().capture.sleep_mode is False
    # Reconciler now converges audio (awake path), not sleep.
    assert rec.reconcile() == "start_audio"
    assert all(c.command.type != "sleep" for c in dispatcher.pending())


def test_low_reported_battery_refuses_a_long_op_in_guardrails():
    """The guardrails' long-op battery floor reads the REPORTED battery from
    the provider (P2): reported 0.07 clears the 0.05 quick floor but not the
    0.10 long-op floor, so record_video>10s is refused on battery grounds."""
    provider = ReportedCapabilityProvider()
    provider.report(battery_pct=0.07, state="active", wake_reason=None)
    guardrails = StrictCommandGuardrails(
        capabilities=CapabilitySet(
            camera=True, microphone=True, retrospective_buffer=True,
        ),
        resources=provider.resources(),
    )
    out = guardrails.check(
        ValidatedCommand(
            command_type="record_video",
            params={"duration_s": 30},
            idempotency_key="k",
            confidence=0.9,
        ),
    )
    assert not out.allowed
    assert "battery" in (out.message or "").lower()