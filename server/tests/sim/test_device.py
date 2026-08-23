"""Tests for the reference device client (the firmware/relay's behaviour in Python).

This is the executable spec of the device side of the protocol: it builds §C.6 audio
packets, opens a session with §E hello, and — the security-critical part — verifies
the Ed25519 signature on every §D command before acking it. A command that fails
verification (forged/tampered by the untrusted relay) is dropped, never acked.
"""

import json
from datetime import datetime, timedelta, timezone

from openrecall_server.commands.model import Command
from openrecall_server.commands.signing import CommandSigner
from openrecall_server.ingest.audio_packet import AudioPacket, PacketType, VadState
from openrecall_server.protocol.messages import (
    Ack,
    CommandMessage,
    Hello,
    Telemetry,
    TranscriptMsg,
    parse_control,
)
from openrecall_server.sim.device import DeviceClient

NOW = datetime.now(timezone.utc)


def a_signed_command_message(signer: CommandSigner, command_id: str, session_id: str = "s1"):
    command = Command(
        command_id=command_id,
        session_id=session_id,
        type="capture_photo",
        params={},
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    wire = signer.sign(command).to_wire()
    return CommandMessage(session_id=session_id, payload=wire["payload"], sig=wire["sig"])


def test_hello_opens_the_session_at_the_start_cursor():
    signer = CommandSigner.generate()
    client = DeviceClient("s1", signer.public_key_bytes, start_seq=7)

    assert json.loads(client.hello()) == json.loads(
        Hello(session_id="s1", start_seq=7).model_dump_json()
    )


def test_audio_packets_are_valid_c6_with_increasing_seq():
    client = DeviceClient("s1", CommandSigner.generate().public_key_bytes)

    p0 = AudioPacket.parse(client.next_audio_packet([b"opus0"]))
    p1 = AudioPacket.parse(client.next_audio_packet([b"opus1"], vad=VadState.GAP_MARKER))

    assert (p0.chunk_seq, p0.ptype, p0.frames) == (0, PacketType.MEMORY_CHUNK, [b"opus0"])
    assert p1.chunk_seq == 1
    assert p1.is_silence_gap is True


def test_transcript_message_is_recorded_with_no_reply():
    client = DeviceClient("s1", CommandSigner.generate().public_key_bytes)

    replies = client.on_message(
        TranscriptMsg(session_id="s1", text="hello world", duration_ms=5000).model_dump_json()
    )

    assert replies == []
    assert client.transcripts == ["hello world"]


def test_valid_command_is_verified_and_acked():
    signer = CommandSigner.generate()
    client = DeviceClient("s1", signer.public_key_bytes)
    msg = a_signed_command_message(signer, "c1")

    replies = client.on_message(msg.model_dump_json())

    assert [c.command_id for c in client.verified_commands] == ["c1"]
    assert len(replies) == 1
    assert json.loads(replies[0]) == {
        "type": "command_ack",
        "session_id": "s1",
        "command_id": "c1",
    }


def test_forged_command_is_rejected_and_not_acked():
    real_server = CommandSigner.generate()
    attacker = CommandSigner.generate()
    client = DeviceClient("s1", real_server.public_key_bytes)
    # signed by the attacker, not the real server the device trusts
    forged = a_signed_command_message(attacker, "evil")

    replies = client.on_message(forged.model_dump_json())

    assert replies == []
    assert client.verified_commands == []
    assert client.rejected_commands == 1


def test_ack_message_from_server_is_accepted_silently():
    client = DeviceClient("s1", CommandSigner.generate().public_key_bytes)
    assert client.on_message(Ack(session_id="s1", next_seq=3).model_dump_json()) == []


def test_device_client_telemetry_frame_parses():
    """P2: DeviceClient.telemetry() emits a Telemetry frame the server parses."""
    client = DeviceClient("s1", b"\x00" * 32)
    frame = client.telemetry(battery_pct=0.85, state="active", wake_reason="button")
    msg = parse_control(frame)
    assert isinstance(msg, Telemetry)
    assert msg.battery_pct == 0.85
    assert msg.state == "active"
    assert msg.wake_reason == "button"


def test_device_client_upload_snapshot_request_builds_request():
    """P3: DeviceClient.upload_snapshot_request builds a raw-body JPEG POST."""
    client = DeviceClient("sim-1", b"\x00" * 32)
    req = client.upload_snapshot_request(
        "http://127.0.0.1:8080/media/snapshots",
        rel_ts_ms=3000, session_id="sim-1", image=b"\xff\xd8img", token="tok",
    )
    assert req["url"].startswith("http://127.0.0.1:8080/media/snapshots?")
    assert "rel_ts_ms=3000" in req["url"]
    assert "session_id=sim-1" in req["url"]
    assert req["body"] == b"\xff\xd8img"
    assert req["headers"]["Authorization"] == "Bearer tok"
    assert req["headers"]["Content-Type"] == "image/jpeg"
