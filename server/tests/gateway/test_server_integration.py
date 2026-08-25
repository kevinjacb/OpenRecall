"""End-to-end integration test of the gateway over a real WebSocket.

Covers the one piece the router unit tests cannot: the async socket loop in
``serve`` — accepting a connection, routing text + binary frames, and sending
replies back. Uses a fake pipeline factory so no model/codec is needed.
"""

import asyncio
import json
import struct
import time

import pytest
import websockets

from openrecall_server.events.store import InMemoryEventStore
from openrecall_server.gateway.adapter import serve
from openrecall_server.ingest.audio_packet import PacketType, VadState
from openrecall_server.ingest.pipeline import AudioIngestPipeline
from openrecall_server.ingest.reassembler import SessionReassembler

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
                "speaker": None,
                "speaker_confidence": None,
                "speaker_assignment": None,
                "speaker_name": None,
                "is_wearer": False,
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


class _SlowTranscriber:
    """Sleeps 0.3s per call to simulate a slow model inference."""

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        time.sleep(0.3)
        return "slow result"


def _slow_factory(start_seq: int) -> AudioIngestPipeline:
    return AudioIngestPipeline(
        reassembler=SessionReassembler(start_seq=start_seq),
        decoder=FakeDecoder(),
        transcriber=_SlowTranscriber(),
        hop_ms=100,
        window_ms=100,
        sample_rate=16000,
    )


async def test_ws_loop_stays_responsive_during_slow_inference():
    """S1: the WS receive loop stays responsive while inference runs on the
    worker thread. A second ``hello`` (a fast control frame) is acked while
    the first connection's slow transcribe is still running — proving the
    worker decoupled inference from the receive loop."""
    port = 8792
    server_task = asyncio.create_task(
        serve(_slow_factory, host="127.0.0.1", port=port)
    )
    try:
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

            # Send audio (triggers a slow 0.3s transcribe on the worker).
            await conn.send(audio_bytes(0, n_frames=5))

            # While the transcribe is running, send a second hello on a NEW
            # connection. If the server's event loop were blocked by the
            # slow inference, this connection would time out. With the worker,
            # the loop is free and the new connection is accepted + acked
            # promptly.
            t0 = time.monotonic()
            async with await websockets.connect(f"ws://127.0.0.1:{port}") as conn2:
                await conn2.send(
                    '{"type": "hello", "session_id": "s2", "start_seq": 0}'
                )
                ack2 = json.loads(await conn2.recv())
            elapsed = time.monotonic() - t0

            assert ack2 == {"type": "ack", "session_id": "s2", "next_seq": 0}
            # The second connection was acked in well under the 0.3s
            # transcribe time — the loop was not blocked.
            assert elapsed < 0.25, (
                f"second connection took {elapsed:.3f}s — loop was blocked "
                f"by the slow transcribe"
            )

            # The slow transcript still arrives eventually.
            transcript = json.loads(await conn.recv())
            assert transcript["type"] == "transcript"
            assert transcript["text"] == "slow result"
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task
