"""Tests for the command dispatcher: at-least-once delivery, idempotency, expiry.

The dispatcher signs and tracks outstanding commands. Delivery is at-least-once, so
it re-offers unacknowledged commands until they are acked or expire. Acks are
idempotent (the device may ack more than once), and issuing the same command_id
twice does not duplicate it.
"""

from datetime import datetime, timedelta, timezone

from sense_server.commands.dispatcher import CommandDispatcher
from sense_server.commands.model import Command
from sense_server.commands.signing import CommandSigner, verify_command

T0 = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def a_command(command_id: str, ttl_s: int = 30) -> Command:
    return Command(
        command_id=command_id,
        session_id="s1",
        type="capture_photo",
        params={},
        issued_at=T0,
        expires_at=T0 + timedelta(seconds=ttl_s),
    )


def make():
    clock = FakeClock(T0)
    signer = CommandSigner.generate()
    return CommandDispatcher(signer, clock=clock), signer, clock


def test_issued_command_is_pending_and_signed():
    disp, signer, _ = make()

    signed = disp.issue(a_command("c1"))

    assert verify_command(signed, signer.public_key_bytes) is True
    assert [s.command.command_id for s in disp.pending()] == ["c1"]


def test_ack_removes_from_pending_and_is_idempotent():
    disp, _, _ = make()
    disp.issue(a_command("c1"))

    disp.ack("c1")
    disp.ack("c1")  # duplicate ack is harmless
    disp.ack("unknown")  # acking something we never issued is harmless

    assert disp.pending() == []


def test_unacked_commands_remain_pending_for_redelivery():
    disp, _, _ = make()
    disp.issue(a_command("c1"))
    disp.issue(a_command("c2"))
    disp.ack("c1")

    assert [s.command.command_id for s in disp.pending()] == ["c2"]


def test_expired_commands_drop_out_of_pending():
    disp, _, clock = make()
    disp.issue(a_command("c1", ttl_s=30))

    assert [s.command.command_id for s in disp.pending()] == ["c1"]

    clock.t = T0 + timedelta(seconds=30)  # reach expiry
    assert disp.pending() == []


def test_issuing_the_same_command_id_twice_does_not_duplicate():
    disp, _, _ = make()
    disp.issue(a_command("c1"))
    disp.issue(a_command("c1"))

    assert [s.command.command_id for s in disp.pending()] == ["c1"]
