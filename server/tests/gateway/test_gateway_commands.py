"""Tests for server->device command delivery through the gateway.

Signed §D commands ride back on the control plane: the device receives pending
commands on ``hello`` (initial sync) and, after acking one, pulls the remainder.
Delivery is at-least-once and per-session; acks are relayed to the dispatcher. The
device verifies each command's signature (reconstructed here to prove the envelope
survives the gateway intact).
"""

from datetime import datetime, timedelta, timezone

from sense_server.commands.dispatcher import CommandDispatcher
from sense_server.commands.model import Command
from sense_server.commands.signing import CommandSigner, SignedCommand, verify_command
from sense_server.gateway.core import GatewayCore
from sense_server.ingest.pipeline import AudioIngestPipeline
from sense_server.ingest.reassembler import SessionReassembler
from sense_server.protocol.messages import Ack, CommandAck, CommandMessage, Hello

NOW = datetime.now(timezone.utc)


class FakeDecoder:
    def decode(self, frame: bytes) -> bytes:
        return b"\x00" * 640


class FakeTranscriber:
    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        return "x"


def factory(start_seq: int) -> AudioIngestPipeline:
    return AudioIngestPipeline(
        reassembler=SessionReassembler(start_seq=start_seq),
        decoder=FakeDecoder(),
        transcriber=FakeTranscriber(),
        window_ms=100,
        sample_rate=16000,
    )


def a_command(command_id: str, session_id: str = "s1") -> Command:
    return Command(
        command_id=command_id,
        session_id=session_id,
        type="capture_photo",
        params={},
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )


def reconstruct(msg: CommandMessage) -> SignedCommand:
    return SignedCommand.from_wire({"payload": msg.payload, "sig": msg.sig})


def make():
    signer = CommandSigner.generate()
    dispatcher = CommandDispatcher(signer)
    core = GatewayCore(pipeline_factory=factory, dispatcher=dispatcher)
    return core, dispatcher, signer


def test_hello_delivers_pending_commands_after_the_ack():
    core, dispatcher, signer = make()
    dispatcher.issue(a_command("c1"))
    dispatcher.issue(a_command("c2"))

    out = core.on_control(Hello(session_id="s1", start_seq=0))

    assert out[0] == Ack(session_id="s1", next_seq=0)
    commands = out[1:]
    assert all(isinstance(m, CommandMessage) for m in commands)
    assert [reconstruct(m).command.command_id for m in commands] == ["c1", "c2"]
    # the signature survived the gateway and verifies under the server key
    assert verify_command(reconstruct(commands[0]), signer.public_key_bytes) is True


def test_command_ack_is_relayed_and_remaining_commands_are_returned():
    core, dispatcher, _ = make()
    dispatcher.issue(a_command("c1"))
    dispatcher.issue(a_command("c2"))
    core.on_control(Hello(session_id="s1", start_seq=0))

    out = core.on_control(CommandAck(session_id="s1", command_id="c1"))

    assert [reconstruct(m).command.command_id for m in out] == ["c2"]
    assert dispatcher.pending()  # c2 still outstanding
    assert all(s.command.command_id != "c1" for s in dispatcher.pending())


def test_only_this_sessions_commands_are_delivered():
    core, dispatcher, _ = make()
    dispatcher.issue(a_command("c1", session_id="s1"))
    dispatcher.issue(a_command("cX", session_id="s2"))

    out = core.on_control(Hello(session_id="s1", start_seq=0))

    assert [reconstruct(m).command.command_id for m in out[1:]] == ["c1"]
