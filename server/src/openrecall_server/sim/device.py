"""Reference device client — the wearable/relay side of the protocol, in Python.

This is the executable spec the firmware (C) and the Android relay (Kotlin) mirror.
It is transport-agnostic: it produces the bytes/strings to send and consumes the
frames received, so it is fully unit-testable and also drives a real WebSocket via
:mod:`openrecall_server.sim.runner`.

Security-critical behaviour: the device trusts only the server's provisioned public
key. Every §D command is signature-verified before it is acked or acted on; a forged
or tampered command (the untrusted relay's threat model) is dropped, never acked.
"""

from __future__ import annotations

import json

from ..commands.model import Command
from ..commands.signing import SignedCommand, verify_command
from ..ingest.audio_packet import AudioPacket, PacketType, VadState
from ..protocol.messages import Bye, CommandAck, Hello


class DeviceClient:
    def __init__(self, session_id: str, server_public_key: bytes, start_seq: int = 0) -> None:
        self.session_id = session_id
        self._server_public_key = server_public_key
        self._seq = start_seq
        self.transcripts: list[str] = []
        self.verified_commands: list[Command] = []
        self.rejected_commands = 0

    # --- outbound (device -> server) ---

    def hello(self) -> str:
        return Hello(session_id=self.session_id, start_seq=self._seq).model_dump_json()

    def bye(self) -> str:
        return Bye(session_id=self.session_id).model_dump_json()

    def next_audio_packet(
        self,
        frames: list[bytes],
        *,
        vad: VadState = VadState.SPEECH,
        rel_ts_ms: int = 0,
        ptype: PacketType = PacketType.MEMORY_CHUNK,
        flags: int = 0,
    ) -> bytes:
        """Build the next §C.6 packet and advance the chunk_seq cursor."""
        packet = AudioPacket(
            version=1,
            ptype=ptype,
            chunk_seq=self._seq,
            rel_ts_ms=rel_ts_ms,
            vad_state=vad,
            flags=flags,
            frames=frames,
        )
        self._seq += 1
        return packet.encode()

    # --- inbound (server -> device) ---

    def on_message(self, message: str | bytes) -> list[str]:
        """Handle a server §E message; return any reply frames (JSON strings)."""
        data = json.loads(message)
        kind = data.get("type")

        if kind == "transcript":
            self.transcripts.append(data["text"])
            return []
        if kind == "command":
            return self._on_command(data)
        # ack / request_chunks and anything else: nothing to send back here
        return []

    def _on_command(self, data: dict) -> list[str]:
        signed = SignedCommand.from_wire({"payload": data["payload"], "sig": data["sig"]})
        if not verify_command(signed, self._server_public_key):
            self.rejected_commands += 1  # forged/tampered — drop, do NOT ack
            return []
        self.verified_commands.append(signed.command)
        return [
            CommandAck(
                session_id=self.session_id, command_id=signed.command.command_id
            ).model_dump_json()
        ]
