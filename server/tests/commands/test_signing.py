"""Tests for Ed25519 signing/verification of device commands.

Trust model: the server holds the private key and signs; the device holds only the
server's public key and verifies. The Android relay is untrusted and must not be
able to forge or tamper with a command without detection. The device trusts the
signed *payload* bytes and parses the command from them.
"""

from datetime import datetime, timedelta, timezone

from openrecall_server.commands.model import Command
from openrecall_server.commands.signing import (
    CommandSigner,
    SignedCommand,
    load_or_create_signer,
    verify_command,
)

T0 = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


def a_command(command_id: str = "c1", text: str = "hello") -> Command:
    return Command(
        command_id=command_id,
        session_id="s1",
        type="display_text",
        params={"text": text},
        issued_at=T0,
        expires_at=T0 + timedelta(seconds=30),
    )


def test_sign_then_verify_with_the_servers_public_key():
    signer = CommandSigner.generate()

    signed = signer.sign(a_command())

    assert verify_command(signed, signer.public_key_bytes) is True
    assert len(signer.public_key_bytes) == 32  # raw Ed25519 public key


def test_verification_fails_with_a_different_key():
    signed = CommandSigner.generate().sign(a_command())
    attacker = CommandSigner.generate()

    assert verify_command(signed, attacker.public_key_bytes) is False


def test_tampering_with_the_payload_breaks_verification():
    signer = CommandSigner.generate()
    signed = signer.sign(a_command(text="hello"))

    # relay swaps in a different command body but keeps the original signature
    forged = SignedCommand(
        command=a_command(text="WIRE ME MONEY"),
        payload=a_command(text="WIRE ME MONEY").canonical_bytes(),
        signature=signed.signature,
    )

    assert verify_command(forged, signer.public_key_bytes) is False


def test_wire_round_trip_preserves_command_and_signature():
    signer = CommandSigner.generate()
    signed = signer.sign(a_command(text="say hi"))

    restored = SignedCommand.from_wire(signed.to_wire())

    assert restored.command == signed.command
    assert verify_command(restored, signer.public_key_bytes) is True


def test_signer_can_be_persisted_and_reloaded_from_private_bytes():
    signer = CommandSigner.generate()
    reloaded = CommandSigner.from_private_bytes(signer.private_key_bytes)

    # same identity: a command signed by the reload verifies under the original pubkey
    signed = reloaded.sign(a_command())
    assert reloaded.public_key_bytes == signer.public_key_bytes
    assert verify_command(signed, signer.public_key_bytes) is True


def test_load_or_create_signer_persists_a_stable_identity(tmp_path):
    path = tmp_path / "keys" / "server_ed25519.key"

    first = load_or_create_signer(path)  # generates + writes
    second = load_or_create_signer(path)  # loads the same key

    assert path.exists()
    assert first.public_key_bytes == second.public_key_bytes
