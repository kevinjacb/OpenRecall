"""Phase 4 — unbound command delivery and reconcile-on-hello (spec D3, §4.2).

Before this, `_pending_commands` required `command.session_id == self._session_id`.
Session ids are relay-minted UUIDs that are never surfaced over HTTP, so an
HTTP-issued command could not be targeted at anything and was undeliverable —
which is why the Settings toggles had no working path to the device.
"""
from __future__ import annotations

from datetime import timedelta

from opensapien_server.commands.dispatcher import CommandDispatcher
from opensapien_server.commands.model import Command
from opensapien_server.commands.signing import CommandSigner
from opensapien_server.contracts.clock import FakeClock
from opensapien_server.gateway.core import GatewayCore
from opensapien_server.ingest.pipeline import AudioIngestPipeline
from opensapien_server.ingest.reassembler import SessionReassembler
from opensapien_server.protocol.messages import CommandAck, CommandMessage, Hello
from opensapien_server.settings.model import SettingsDocument
from opensapien_server.settings.reconciler import DeviceReconciler
from opensapien_server.settings.store import InMemorySettingsStore

from .test_core_index_hook import FakeDecoder, FakeTranscriber


def _decoded(message: CommandMessage) -> Command:
    """The command inside a §E command frame. The wire carries the signed
    payload verbatim, so this is also what the device parses."""
    return Command.model_validate_json(message.payload)


def _factory(start_seq: int) -> AudioIngestPipeline:
    return AudioIngestPipeline(
        reassembler=SessionReassembler(start_seq=start_seq),
        decoder=FakeDecoder(),
        transcriber=FakeTranscriber(),
        hop_ms=20, window_ms=100, sample_rate=16000,
    )


def _dispatcher(clock):
    return CommandDispatcher(CommandSigner.generate(), clock=clock.now)


def _command(clock, *, session_id="", command_id="c1", type="start_audio"):
    now = clock.now()
    return Command(
        command_id=command_id,
        session_id=session_id,
        type=type,
        params={},
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
        idempotency_key=f"key-{command_id}",
    )


# ---- unbound delivery (D3) --------------------------------------------------


def test_an_unbound_command_is_delivered_to_whatever_session_connects():
    clock = FakeClock()
    dispatcher = _dispatcher(clock)
    dispatcher.issue(_command(clock, session_id=""))
    core = GatewayCore(pipeline_factory=_factory, dispatcher=dispatcher)

    out = core.on_control(Hello(session_id="relay-uuid-nobody-knows", start_seq=0))

    commands = [m for m in out if isinstance(m, CommandMessage)]
    assert [_decoded(c).command_id for c in commands] == ["c1"]


def test_the_live_session_id_is_stamped_into_the_outgoing_message():
    clock = FakeClock()
    dispatcher = _dispatcher(clock)
    dispatcher.issue(_command(clock, session_id=""))
    core = GatewayCore(pipeline_factory=_factory, dispatcher=dispatcher)

    out = core.on_control(Hello(session_id="s-live", start_seq=0))

    (command,) = [m for m in out if isinstance(m, CommandMessage)]
    assert command.session_id == "s-live"


def test_the_signed_payload_keeps_the_empty_session_id():
    """The signature must still verify, which it only does if the signed
    bytes are untouched. Safe because the firmware never reads the field."""
    clock = FakeClock()
    dispatcher = _dispatcher(clock)
    dispatcher.issue(_command(clock, session_id=""))
    core = GatewayCore(pipeline_factory=_factory, dispatcher=dispatcher)

    core.on_control(Hello(session_id="s-live", start_seq=0))

    assert dispatcher.pending()[0].command.session_id == ""


def test_a_bound_command_still_only_reaches_its_own_session():
    clock = FakeClock()
    dispatcher = _dispatcher(clock)
    dispatcher.issue(_command(clock, session_id="s-other"))
    core = GatewayCore(pipeline_factory=_factory, dispatcher=dispatcher)

    out = core.on_control(Hello(session_id="s-mine", start_seq=0))

    assert [m for m in out if isinstance(m, CommandMessage)] == []


# ---- reconcile on hello (§4.2) ----------------------------------------------


def _core_with_reconciler(*, audio_enabled=True, device_state=None):
    clock = FakeClock()
    settings = InMemorySettingsStore(
        SettingsDocument.model_validate({"capture": {"audio_enabled": audio_enabled}}),
    )
    if device_state is not None:
        settings.put_device_state(device_state)
    dispatcher = _dispatcher(clock)
    from opensapien_server.contracts.id_generator import DeterministicIdGenerator

    reconciler = DeviceReconciler(
        settings=settings, dispatcher=dispatcher,
        ids=DeterministicIdGenerator(), clock=clock,
    )
    core = GatewayCore(
        pipeline_factory=_factory, dispatcher=dispatcher, reconciler=reconciler,
    )
    return core, settings, dispatcher


def test_hello_reconciles_and_delivers_in_the_same_round_trip():
    """The reconcile command should ride out on this hello rather than wait
    for the next ack."""
    core, _settings, _dispatcher = _core_with_reconciler(audio_enabled=False)

    out = core.on_control(Hello(session_id="s1", start_seq=0))

    (message,) = [m for m in out if isinstance(m, CommandMessage)]
    assert _decoded(message).type == "stop_audio"


def test_hello_issues_nothing_when_already_converged():
    core, _settings, dispatcher = _core_with_reconciler(
        audio_enabled=True, device_state={"audio_enabled": True},
    )

    out = core.on_control(Hello(session_id="s1", start_seq=0))

    assert [m for m in out if isinstance(m, CommandMessage)] == []
    assert dispatcher.pending() == []


def test_an_ack_records_the_state_the_device_reached():
    core, settings, _dispatcher = _core_with_reconciler(audio_enabled=False)
    out = core.on_control(Hello(session_id="s1", start_seq=0))
    (message,) = [m for m in out if isinstance(m, CommandMessage)]

    core.on_control(CommandAck(session_id="s1", command_id=_decoded(message).command_id))

    assert settings.get_device_state()["audio_enabled"] is False


def test_a_reconnect_after_an_ack_issues_nothing_more():
    """The command storm this guards against is proportional to link
    quality — worst exactly when the link is worst."""
    core, _settings, dispatcher = _core_with_reconciler(audio_enabled=False)
    out = core.on_control(Hello(session_id="s1", start_seq=0))
    (message,) = [m for m in out if isinstance(m, CommandMessage)]
    core.on_control(CommandAck(session_id="s1", command_id=_decoded(message).command_id))

    for _ in range(5):
        core.on_disconnect()
        core.on_control(Hello(session_id="s1", start_seq=0))

    assert dispatcher.pending() == []


def test_a_failing_reconciler_does_not_refuse_the_connection():
    class Exploding:
        def reconcile(self):
            raise RuntimeError("boom")

        def note_acked(self, command_type):
            raise RuntimeError("boom")

    core = GatewayCore(pipeline_factory=_factory, reconciler=Exploding())

    out = core.on_control(Hello(session_id="s1", start_seq=0))

    assert out  # the ack still went out


def test_a_core_without_a_reconciler_works_as_before():
    core = GatewayCore(pipeline_factory=_factory)

    assert core.on_control(Hello(session_id="s1", start_seq=0))
