"""GatewayCore on_telemetry — updates provider, clears desired sleep on button-wake (P2).

Telemetry arrives as a §E control frame and is dispatched by `on_control`'s
isinstance chain. The handler (a) reports battery/state/wake_reason to the
capability provider, and (b) on `wake_reason == "button"` clears desired
`capture.sleep_mode` so the reconciler does not immediately re-issue `sleep`
(D2: a button wake is the authoritative "turn on").

These tests use a `_NoopPipeline` (Telemetry does not touch the audio path)
and isolate from `tests/gateway/test_core.py` to keep the P2 slice separate.
"""

from openrecall_server.agent.capability import ReportedCapabilityProvider
from openrecall_server.gateway.core import GatewayCore
from openrecall_server.protocol.messages import Telemetry
from openrecall_server.settings.model import SettingsDocument
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


def _core(*, sleep_mode: bool = False):
    settings = InMemorySettingsStore(
        SettingsDocument.model_validate({"capture": {"sleep_mode": sleep_mode}}),
    )
    provider = ReportedCapabilityProvider()
    core = GatewayCore(
        pipeline_factory=lambda start_seq: _NoopPipeline(),
        settings=settings,
        capability_provider=provider,
    )
    return core, provider, settings


def test_telemetry_updates_provider():
    core, provider, _settings = _core()
    core.on_control(
        Telemetry(
            session_id="s1",
            battery_pct=0.7,
            state="active",
            wake_reason="none",
        ),
    )
    assert provider.resources().battery_pct == 0.7
    assert provider.state() == "active"


def test_button_wake_clears_desired_sleep_mode():
    core, _provider, settings = _core(sleep_mode=True)
    core.on_control(
        Telemetry(
            session_id="s1",
            battery_pct=0.9,
            state="active",
            wake_reason="button",
        ),
    )
    assert settings.get().capture.sleep_mode is False


def test_non_button_telemetry_does_not_clear_sleep_mode():
    core, _provider, settings = _core(sleep_mode=True)
    core.on_control(
        Telemetry(
            session_id="s1",
            battery_pct=0.9,
            state="active",
            wake_reason="none",
        ),
    )
    assert settings.get().capture.sleep_mode is True


def test_telemetry_without_settings_or_provider_is_a_noop():
    core = GatewayCore(pipeline_factory=lambda s: _NoopPipeline())
    # must not raise — the no-op-when-None discipline
    core.on_control(
        Telemetry(session_id="s1", battery_pct=0.5, state="sleeping"),
    )