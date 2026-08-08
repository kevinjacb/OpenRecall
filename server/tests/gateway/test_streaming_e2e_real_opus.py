"""Integration test: real Opus decoding + streaming Whisper backend.

The other streaming tests use a :class:`FakeOpusDecoder` that returns
PCM directly, bypassing the real Opus codec. This test goes one
step further: it wires the real :class:`OpusStreamDecoder` against
a pre-encoded Opus frame (the §C.6 packet shape the firmware actually
sends), so the full chain is exercised. The mlx-whisper backend is
still faked so the test stays unit-fast.
"""
from __future__ import annotations

import struct

import pytest

from opensapien_server.ingest.audio_packet import AudioPacket, PacketType, VadState
from opensapien_server.ingest.opus_decoder import OpusStreamDecoder
from opensapien_server.ingest.pipeline import AudioIngestPipeline
from opensapien_server.ingest.reassembler import SessionReassembler
from opensapien_server.ingest.streaming_transcriber import streaming_from_tokens
from opensapien_server.ingest.whisper_streaming import WhisperStreamingBackend


def _build_opus_packet_with_real_opus_frame(chunk_seq: int) -> bytes:
    """Build a §C.6 packet with a REAL Opus frame in the payload.

    We import :mod:`opuslib` lazily and skip the test if it's not
    installed (the dev environment may not have it). When skipped,
    the gateway's other tests still cover the streaming path with
    a fake decoder.
    """
    try:
        import opuslib
    except ImportError:
        pytest.skip("opuslib not installed; skipping real-Opus integration test")

    # 20ms of silence at 16 kHz mono int16 = 320 samples = 640 bytes
    import numpy as np
    pcm = np.zeros(320, dtype=np.int16).tobytes()
    encoder = opuslib.Encoder(16000, 1, opuslib.APPLICATION_AUDIO)
    opus_frame = encoder.encode(pcm, 320)
    frames = [bytes(opus_frame)]
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


class ScriptedMlx:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self._idx = 0

    def __call__(self, audio, path_or_hf_repo: str, **kwargs):
        if self._idx >= len(self._responses):
            return {"text": "", "segments": []}
        out = self._responses[self._idx]
        self._idx += 1
        return out


def test_real_opus_plus_streaming_whisper_produces_coherent_output():
    """5 audio packets (each 20ms) → 5 streaming transcripts (one
    per hop, since hop=20ms=1 frame). The fake mlx-whisper returns
    a unique word per call with realistic timestamps (each new word
    starts at the trailing edge of the previous hop, past the
    committed cursor).
    """
    mlx = ScriptedMlx([
        # Hop 1: buffer 0-20ms. "hello" at 0-15ms.
        {"text": "hello", "segments": [
            {"text": "hello", "start": 0.0, "end": 0.015, "words": [
                {"word": "hello", "start": 0.0, "end": 0.015}
            ]}
        ]},
        # Hop 2: buffer 0-40ms. "hello" at 0-15ms (overlap) + "world" at 20-35ms.
        {"text": "helloworld", "segments": [
            {"text": "helloworld", "start": 0.0, "end": 0.035, "words": [
                {"word": "hello", "start": 0.0, "end": 0.015},
                {"word": "world", "start": 0.020, "end": 0.035},
            ]}
        ]},
        # Hop 3: buffer 0-60ms. + "foo" at 40-55ms.
        {"text": "helloworldfoo", "segments": [
            {"text": "helloworldfoo", "start": 0.0, "end": 0.055, "words": [
                {"word": "hello", "start": 0.0, "end": 0.015},
                {"word": "world", "start": 0.020, "end": 0.035},
                {"word": "foo", "start": 0.040, "end": 0.055},
            ]}
        ]},
        # Hop 4: + "bar" at 60-75ms.
        {"text": "helloworldfoobar", "segments": [
            {"text": "helloworldfoobar", "start": 0.0, "end": 0.075, "words": [
                {"word": "hello", "start": 0.0, "end": 0.015},
                {"word": "world", "start": 0.020, "end": 0.035},
                {"word": "foo", "start": 0.040, "end": 0.055},
                {"word": "bar", "start": 0.060, "end": 0.075},
            ]}
        ]},
        # Hop 5: + "baz" at 80-95ms.
        {"text": "helloworldfoobarbaz", "segments": [
            {"text": "helloworldfoobarbaz", "start": 0.0, "end": 0.095, "words": [
                {"word": "hello", "start": 0.0, "end": 0.015},
                {"word": "world", "start": 0.020, "end": 0.035},
                {"word": "foo", "start": 0.040, "end": 0.055},
                {"word": "bar", "start": 0.060, "end": 0.075},
                {"word": "baz", "start": 0.080, "end": 0.095},
            ]}
        ]},
    ])
    backend = WhisperStreamingBackend(mlx_transcribe=mlx)
    streamer = streaming_from_tokens(backend, sample_rate=16000, hop_ms=20, window_ms=100)
    pipeline = AudioIngestPipeline(
        reassembler=SessionReassembler(0),
        decoder=OpusStreamDecoder(),  # REAL Opus decoding
        transcriber=streamer,
        hop_ms=20,
        window_ms=100,
        sample_rate=16000,
    )
    transcripts = []
    for seq in range(5):
        packet = AudioPacket.parse(_build_opus_packet_with_real_opus_frame(seq))
        transcripts.extend(pipeline.ingest(packet))

    # 5 unique transcripts, one per hop. The real Opus decoder
    # produced 320 bytes of PCM per frame; the streaming wrapper
    # saw 320 bytes (1 hop = 1 frame) and committed each.
    text = "".join(t.text.strip() for t in transcripts)
    assert text.count("hello") == 1
    assert text.count("world") == 1
    assert text.count("foo") == 1
    assert text.count("bar") == 1
    assert text.count("baz") == 1


def _opuc_lib_available() -> bool:
    try:
        import opuslib  # noqa: F401
        return True
    except ImportError:
        return False
