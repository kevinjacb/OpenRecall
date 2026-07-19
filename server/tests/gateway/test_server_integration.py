"""End-to-end integration test of the gateway over a real WebSocket.

Covers the one piece the router unit tests cannot: the async socket loop in
``serve`` — accepting a connection, routing text + binary frames, and sending
replies back. Uses a fake pipeline factory so no model/codec is needed.
"""

import asyncio
import json
import struct

import pytest
import websockets

from sense_server.events.store import InMemoryEventStore
from sense_server.gateway.adapter import serve
from sense_server.ingest.audio_packet import PacketType, VadState
from sense_server.ingest.pipeline import AudioIngestPipeline
from sense_server.ingest.reassembler import SessionReassembler

pytestmark = pytest.mark.asyncio


class FakeDecoder:
    def decode(self, frame: bytes) -> bytes:
        return b"\x00" * 640


class FakeTranscriber:
    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        return "hello world"


def fake_factory(start_seq: int) -> AudioIngestPipeline:
    return AudioIngestPipeline(
        reassembler=SessionReassembler(start_seq=start_seq),
        decoder=FakeDecoder(),
        transcriber=FakeTranscriber(),
        # hop_ms == window_ms produces one transcript per full window
        # (matches the pre-streaming test expectation: 5 frames in one
        # 100ms window -> one 100ms transcript). The streaming pipeline
        # would otherwise emit hop-sized transcripts, not window-sized.
        hop_ms=100,
        window_ms=100,  # 5 frames per window
        sample_rate=16000,
    )


def audio_bytes(chunk_seq: int, n_frames: int) -> bytes:
    frames = [bytes([chunk_seq & 0xFF])] * n_frames
    header = struct.pack(
        "<BIIBBB",
        (1 << 4) | PacketType.MEMORY_CHUNK,
        chunk_seq,
        chunk_seq * 20,
        VadState.SPEECH,
        len(frames),
        0,
    )
    return header + b"".join(struct.pack("<B", len(f)) + f for f in frames)


async def test_full_session_over_a_real_socket():
    port = 8791
    store = InMemoryEventStore()
    server_task = asyncio.create_task(
        serve(fake_factory, host="127.0.0.1", port=port, event_store=store)
    )
    try:
        # wait for the listener to come up
        for _ in range(50):
            try:
                conn = await websockets.connect(f"ws://127.0.0.1:{port}")
                break
            except OSError:
                await asyncio.sleep(0.02)
        else:
            pytest.fail("server did not start")

        async with conn:
            await conn.send('{"type": "hello", "session_id": "s1", "start_seq": 0}')
            ack = json.loads(await conn.recv())
            assert ack == {"type": "ack", "session_id": "s1", "next_seq": 0}

            await conn.send(audio_bytes(0, n_frames=5))  # one full window
            transcript = json.loads(await conn.recv())
            ack2 = json.loads(await conn.recv())
            assert transcript == {
                "type": "transcript",
                "session_id": "s1",
                "text": "hello world",
                "duration_ms": 100,
            }
            assert ack2 == {"type": "ack", "session_id": "s1", "next_seq": 1}

        # the transcribed window was persisted as a durable §F capture event
        events = store.events("s1")
        assert len(events) == 1
        assert events[0].text == "hello world"
        assert events[0].seq == 0
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task
