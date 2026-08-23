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


def _reconciler(*, audio_enabled=True, sleep_mode=False, snapshot_interval_s=None,
                device_state=None):
    capture = {"audio_enabled": audio_enabled, "sleep_mode": sleep_mode}
    if snapshot_interval_s is not None:
        capture["snapshot_interval_s"] = snapshot_interval_s
    settings = InMemorySettingsStore(
        SettingsDocument.model_validate({"capture": capture}),
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


def test_desired_sleep_issues_sleep_command():
    rec, _settings, dispatcher = _reconciler(sleep_mode=True)
    assert rec.reconcile() == "sleep"
    assert [c.command.type for c in dispatcher.pending()] == ["sleep"]


def test_already_sleeping_issues_nothing():
    rec, _settings, dispatcher = _reconciler(
        sleep_mode=True, device_state={"sleep_mode": True},
    )
    assert rec.reconcile() is None
    assert dispatcher.pending() == []


def test_sleep_takes_precedence_over_audio_resume():
    """If the device should be asleep, do not also issue start_audio."""
    rec, _settings, dispatcher = _reconciler(
        audio_enabled=True, sleep_mode=True,
    )
    assert rec.reconcile() == "sleep"
    assert [c.command.type for c in dispatcher.pending()] == ["sleep"]


def test_reconciler_never_issues_wake():
    """Wake is button-only (D2): clearing desired sleep is the telemetry path,
    not a reconciler-issued wake command."""
    rec, _settings, dispatcher = _reconciler(
        sleep_mode=False, device_state={"sleep_mode": True},
    )
    # desired is awake but device known asleep — reconciler does NOT issue wake.
    # It converges audio (the device will resume audio on its own activate path).
    out = rec.reconcile()
    assert out != "wake"
    assert all(c.command.type != "wake" for c in dispatcher.pending())


def test_note_acked_sleep_records_sleeping_state():
    rec, settings, _dispatcher = _reconciler(sleep_mode=True)
    rec.note_acked("sleep")
    assert settings.get_device_state()["sleep_mode"] is True


def test_desired_snapshot_interval_issues_set_snapshot_interval():
    # audio already converged (device_state) so snapshot_interval is the only mismatch
    rec, _settings, dispatcher = _reconciler(
        audio_enabled=True, snapshot_interval_s=120,
        device_state={"audio_enabled": True},
    )
    assert rec.reconcile() == "set_snapshot_interval"
    [cmd] = dispatcher.pending()
    assert cmd.command.type == "set_snapshot_interval"
    assert cmd.command.params == {"seconds": 120}


def test_snapshot_interval_storm_guard_after_ack():
    # 0 = off is still issued (it's a real command); after ack, no storm
    rec, _settings, dispatcher = _reconciler(
        audio_enabled=True, snapshot_interval_s=0,
        device_state={"audio_enabled": True},
    )
    assert rec.reconcile() == "set_snapshot_interval"   # 0=off, still issued
    rec.note_acked("set_snapshot_interval")
    assert rec.reconcile() is None
    # the first command is still pending (not re-issued); no NEW command appended
    assert [c.command.type for c in dispatcher.pending()] == ["set_snapshot_interval"]


def test_snapshot_interval_change_re_issues():
    rec, settings, dispatcher = _reconciler(
        audio_enabled=True, snapshot_interval_s=60,
        device_state={"audio_enabled": True, "snapshot_interval": 60},
    )
    assert rec.reconcile() is None          # converged
    # change desired to 120
    settings.put(SettingsDocument.model_validate(
        {"capture": {"audio_enabled": True, "sleep_mode": False,
                     "snapshot_interval_s": 120}}))
    assert rec.reconcile() == "set_snapshot_interval"
    [cmd] = dispatcher.pending()
    assert cmd.command.params == {"seconds": 120}


def test_sleep_precedence_over_snapshot_interval():
    # sleep_mode=True -> reconcile issues sleep, NOT set_snapshot_interval
    rec, _settings, dispatcher = _reconciler(
        sleep_mode=True, snapshot_interval_s=120,
    )
    assert rec.reconcile() == "sleep"
    assert [c.command.type for c in dispatcher.pending()] == ["sleep"]
