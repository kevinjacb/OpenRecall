"""End-to-end: reference device client <-> real gateway over a real WebSocket.

The whole bidirectional contract with real wire bytes and real crypto:
  * device streams §C.6 audio -> server transcribes -> device receives transcripts,
  * server's pending signed §D command rides back -> device verifies it against the
    server's public key, acts, and acks -> server records the ack,
  * the transcript lands in the durable event store.

No mocks of the protocol — only the model/codec are faked (fixed transcriber).
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from sense_server.commands.dispatcher import CommandDispatcher
from sense_server.commands.model import Command
from sense_server.commands.signing import CommandSigner
from sense_server.events.store import InMemoryEventStore
from sense_server.gateway.adapter import serve
from sense_server.ingest.pipeline import AudioIngestPipeline
from sense_server.ingest.reassembler import SessionReassembler
from sense_server.sim.device import DeviceClient
from sense_server.sim.runner import run_session

pytestmark = pytest.mark.asyncio


class FakeDecoder:
    def decode(self, frame: bytes) -> bytes:
        return b"\x00" * 640


class FakeTranscriber:
    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        return "hello world"


def factory(start_seq: int) -> AudioIngestPipeline:
    return AudioIngestPipeline(
        reassembler=SessionReassembler(start_seq=start_seq),
        decoder=FakeDecoder(),
        transcriber=FakeTranscriber(),
        window_ms=100,  # 5 frames per window
        sample_rate=16000,
    )


async def test_device_streams_audio_and_executes_a_signed_command():
    signer = CommandSigner.generate()
    dispatcher = CommandDispatcher(signer)
    now = datetime.now(timezone.utc)
    dispatcher.issue(
        Command(
            command_id="shoot",
            session_id="s1",
            type="capture_photo",
            params={},
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )
    )
    events = InMemoryEventStore()

    port = 8793
    server = asyncio.create_task(
        serve(factory, host="127.0.0.1", port=port, event_store=events, dispatcher=dispatcher)
    )
    try:
        for _ in range(50):  # wait for listener
            try:
                probe = await __import__("websockets").connect(f"ws://127.0.0.1:{port}")
                await probe.close()
                break
            except OSError:
                await asyncio.sleep(0.02)

        client = DeviceClient("s1", signer.public_key_bytes)
        # one packet of 5 frames == one transcription window
        await run_session(f"ws://127.0.0.1:{port}", client, [[b"opus"] * 5])

        assert client.transcripts == ["hello world"]
        assert [c.command_id for c in client.verified_commands] == ["shoot"]
        assert dispatcher.pending() == []  # the device's ack reached the server
        assert [e.text for e in events.events("s1")] == ["hello world"]  # durably stored
    finally:
        server.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server
