"""Phase 4 — desired-state reconciliation (spec §4.2, D2).

The property that matters most here is the *absence* of commands: a
reconciler that fires on every reconnect without comparing against last-known
state produces a command storm proportional to link quality — worst exactly
when the link is worst.
"""
from __future__ import annotations

from openrecall_server.commands.dispatcher import CommandDispatcher
from openrecall_server.commands.signing import CommandSigner
from openrecall_server.contracts.clock import FakeClock
from openrecall_server.contracts.id_generator import DeterministicIdGenerator
from openrecall_server.settings.model import SettingsDocument
from openrecall_server.settings.reconciler import DeviceReconciler
from openrecall_server.settings.store import InMemorySettingsStore


def _reconciler(*, audio_enabled=True, device_state=None):
    settings = InMemorySettingsStore(
        SettingsDocument.model_validate({"capture": {"audio_enabled": audio_enabled}}),
    )
    if device_state is not None:
        settings.put_device_state(device_state)
    # The dispatcher shares the reconciler's clock, or `pending()` measures a
    # FakeClock-stamped expiry against the wall clock and drops every command
    # as already expired.
    clock = FakeClock()
    dispatcher = CommandDispatcher(CommandSigner.generate(), clock=clock.now)
    return (
        DeviceReconciler(
            settings=settings,
            dispatcher=dispatcher,
            ids=DeterministicIdGenerator(),
            clock=clock,
        ),
        settings,
        dispatcher,
    )


def test_an_unknown_device_state_issues_a_command():
    """Nothing is known about the device yet, so converge it explicitly
    rather than assume it already matches."""
    rec, _settings, dispatcher = _reconciler(audio_enabled=True)

    assert rec.reconcile() == "start_audio"
    assert [c.command.type for c in dispatcher.pending()] == ["start_audio"]


def test_a_matching_device_state_issues_nothing():
    rec, _settings, dispatcher = _reconciler(
        audio_enabled=True, device_state={"audio_enabled": True},
    )

    assert rec.reconcile() is None
    assert dispatcher.pending() == []


def test_a_mismatch_issues_the_command_that_closes_it():
    rec, _settings, dispatcher = _reconciler(
        audio_enabled=False, device_state={"audio_enabled": True},
    )

    assert rec.reconcile() == "stop_audio"
    assert [c.command.type for c in dispatcher.pending()] == ["stop_audio"]


def test_repeated_reconciles_do_not_storm_after_an_ack():
    """The dispatcher dedups on idempotency_key only while a command is
    *unacked*, so without the mismatch guard every reconnect after an ack
    would issue a fresh duplicate."""
    rec, _settings, dispatcher = _reconciler(audio_enabled=True)
    rec.reconcile()
    command_id = dispatcher.pending()[0].command.command_id
    dispatcher.ack(command_id)
    rec.note_acked("start_audio")

    for _ in range(10):
        assert rec.reconcile() is None

    assert dispatcher.pending() == []


def test_reconciles_before_an_ack_dedupe_on_the_key():
    """Two reconciles racing toward the same state are one command."""
    rec, _settings, dispatcher = _reconciler(audio_enabled=True)

    rec.reconcile()
    rec.reconcile()

    assert len(dispatcher.pending()) == 1


def test_note_acked_records_the_state_the_device_reached():
    rec, settings, _dispatcher = _reconciler(audio_enabled=True)

    rec.note_acked("start_audio")

    assert settings.get_device_state()["audio_enabled"] is True


def test_note_acked_ignores_unrelated_commands():
    rec, settings, _dispatcher = _reconciler(audio_enabled=True)

    rec.note_acked("capture_photo")

    assert settings.get_device_state() == {}


def test_state_is_recorded_on_ack_not_on_issue():
    """Recording desired state at issue time would mark an unreachable
    device as converged and stop the reconciler ever retrying — the device
    may never have received the command."""
    rec, settings, dispatcher = _reconciler(audio_enabled=True)

    rec.reconcile()

    assert settings.get_device_state() == {}
    rec.reconcile()  # still trying, because nothing confirmed convergence
    assert len(dispatcher.pending()) == 1  # but deduped into one command


def test_a_reconciler_without_a_dispatcher_is_a_noop():
    rec = DeviceReconciler(settings=InMemorySettingsStore())

    assert rec.reconcile() is None


def test_flipping_the_setting_after_convergence_issues_the_opposite():
    rec, settings, dispatcher = _reconciler(
        audio_enabled=True, device_state={"audio_enabled": True},
    )
    settings.put(
        SettingsDocument.model_validate({"capture": {"audio_enabled": False}}),
    )

    assert rec.reconcile() == "stop_audio"
