"""End-to-end test: the gateway factory wires the streaming backend.

Verifies the full chain: AudioIngestPipeline → StreamingTranscriber
→ WhisperStreamingBackend. The backend uses an injected fake
mlx-whisper so the test stays unit-fast (no MLX dependency).
"""
from __future__ import annotations

import pytest

from opensapien_server.ingest.audio_packet import AudioPacket, PacketType, VadState
from opensapien_server.ingest.opus_decoder import OpusStreamDecoder
from opensapien_server.ingest.pipeline import AudioIngestPipeline
from opensapien_server.ingest.reassembler import SessionReassembler
from opensapien_server.ingest.streaming_transcriber import streaming_from_tokens
from opensapien_server.ingest.transcriber import Transcript
from opensapien_server.ingest.whisper_streaming import WhisperStreamingBackend


def _build_opus_packet(chunk_seq: int, n_frames: int = 1) -> bytes:
    """Build a minimal §C.6 audio packet with N SPEECH frames.

    This bypasses the actual Opus encoder (which would require a
    model dependency). The OpusStreamDecoder in production expects
    real Opus frames; in this test we use a fake decoder (see below)
    that ignores the payload and returns fixed PCM.
    """
    import struct
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


class FakeOpusDecoder:
    """Returns enough PCM bytes for one full hop per call.

    With hop=20ms @ 16kHz int16, one hop is 640 bytes. We return
    640 bytes per call so that one frame == one hop. The default
    320 bytes (one Opus frame = 20ms) makes a single hop require
    two audio frames.
    """

    def __init__(self, bytes_per_frame: int = 640) -> None:
        self._bytes = bytes_per_frame

    def decode(self, frame: bytes) -> bytes:
        return b"\x00" * self._bytes


def _whisper_response_for(words: list[tuple[str, float, float]]) -> dict:
    """Build a single-segment mlx-whisper response from (word, start_s, end_s) tuples."""
    seg_words = [
        {"word": w, "start": s, "end": e} for (w, s, e) in words
    ]
    return {
        "text": "".join(w for (w, _, _) in words),
        "segments": [{
            "text": "".join(w for (w, _, _) in words),
            "start": words[0][1] if words else 0.0,
            "end": words[-1][2] if words else 0.0,
            "words": seg_words,
        }],
    }


class ScriptedMlx:
    """A fake mlx_whisper that returns a different response per call."""

    def __init__(self, scripted: list[dict]) -> None:
        self._scripted = list(scripted)
        self._idx = 0

    def __call__(self, audio, path_or_hf_repo: str, **kwargs):
        if self._idx >= len(self._scripted):
            return {"text": "", "segments": []}
        out = self._scripted[self._idx]
        self._idx += 1
        return out


def test_factory_default_uses_streaming_backend():
    """The factory's default is streaming. The transcriber attached to
    the pipeline is a :class:`StreamingTranscriber`, not a
    :class:`Transcriber`.
    """
    from opensapien_server.ingest.streaming_transcriber import StreamingTranscriber
    from opensapien_server.gateway.adapter import build_pipeline_factory

    factory = build_pipeline_factory(window_ms=100, hop_ms=20)
    pipeline = factory(0)
    assert isinstance(pipeline._streamer, StreamingTranscriber)


def test_factory_legacy_path_still_works():
    """``use_streaming=False`` builds the old :class:`MlxWhisperTranscriber`.

    This path is for tests that pre-date the streaming work and for
    one-off hard-cut transcription use cases.
    """
    from opensapien_server.gateway.adapter import build_pipeline_factory

    factory = build_pipeline_factory(window_ms=100, hop_ms=20, use_streaming=False)
    pipeline = factory(0)
    # The legacy path wraps a str-returning Transcriber into a
    # StreamingTranscriber (via the text factory), so the pipeline's
    # streamer is still a StreamingTranscriber — but the backend
    # is the str adapter, not the WhisperStreamingBackend.
    from opensapien_server.ingest.streaming_transcriber import (
        StreamingTranscriber,
        _TranscriberAdapter,
    )
    assert isinstance(pipeline._streamer, StreamingTranscriber)
    assert isinstance(pipeline._streamer._backend, _TranscriberAdapter)


def test_streaming_end_to_end_pipeline_emits_segments_per_hop():
    """Five 1-hop frames produce 5 segments (one per hop).

    The fake mlx-whisper returns a unique word per call so the
    streaming wrapper's dedup doesn't suppress them. Each word's
    timestamps reflect the realistic position in the rolling
    buffer: a fresh word starts at the trailing edge of the previous
    hop (so it's past the committed cursor and gets emitted).
    """
    mlx = ScriptedMlx([
        # Hop 1: buffer 0-20ms. One word at 0-15ms.
        _whisper_response_for([("hello", 0.0, 0.015)]),
        # Hop 2: buffer 0-40ms. Re-transcribed "hello" at 0-15ms (in
        # overlap zone) plus new "world" at 20-35ms.
        _whisper_response_for([("hello", 0.0, 0.015), ("world", 0.020, 0.035)]),
        # Hop 3: buffer 0-60ms. "hello" + "world" in overlap, new "foo".
        _whisper_response_for([("hello", 0.0, 0.015), ("world", 0.020, 0.035), ("foo", 0.040, 0.055)]),
        # Hop 4: "hello" + "world" + "foo" in overlap, new "bar".
        _whisper_response_for([
            ("hello", 0.0, 0.015), ("world", 0.020, 0.035),
            ("foo", 0.040, 0.055), ("bar", 0.060, 0.075),
        ]),
        # Hop 5: 4 in overlap, new "baz".
        _whisper_response_for([
            ("hello", 0.0, 0.015), ("world", 0.020, 0.035),
            ("foo", 0.040, 0.055), ("bar", 0.060, 0.075), ("baz", 0.080, 0.095),
        ]),
    ])
    backend = WhisperStreamingBackend(mlx_transcribe=mlx)
    streamer = streaming_from_tokens(backend, sample_rate=16000, hop_ms=20, window_ms=100)
    pipeline = AudioIngestPipeline(
        reassembler=SessionReassembler(0),
        decoder=FakeOpusDecoder(),
        transcriber=streamer,
        hop_ms=20,
        window_ms=100,
        sample_rate=16000,
    )
    # Feed 5 audio packets, each with 1 frame.
    transcripts: list[Transcript] = []
    for seq in range(5):
        packet = AudioPacket.parse(_build_opus_packet(seq, n_frames=1))
        transcripts.extend(pipeline.ingest(packet))

    # 5 transcripts, one per hop. Each carries a unique new word.
    assert len(transcripts) == 5
    assert [t.text.strip() for t in transcripts] == ["hello", "world", "foo", "bar", "baz"]


def test_streaming_dedups_overlap_when_whisper_returns_same_word():
    """If mlx-whisper re-transcribes the overlap zone with the same
    word, the streaming wrapper dedups and the word appears exactly
    once in the output.
    """
    # Hop 1: 0-20ms. "hello" at 0-15ms.
    # Hop 2: 0-40ms. "hello" at 0-15ms (overlap) AND " world" at 20-35ms.
    mlx = ScriptedMlx([
        _whisper_response_for([("hello", 0.0, 0.015)]),
        _whisper_response_for([("hello", 0.0, 0.015), (" world", 0.020, 0.035)]),
    ])
    backend = WhisperStreamingBackend(mlx_transcribe=mlx)
    streamer = streaming_from_tokens(backend, sample_rate=16000, hop_ms=20, window_ms=100)
    pipeline = AudioIngestPipeline(
        reassembler=SessionReassembler(0),
        decoder=FakeOpusDecoder(),
        transcriber=streamer,
        hop_ms=20,
        window_ms=100,
        sample_rate=16000,
    )
    transcripts: list[Transcript] = []
    for seq in range(2):
        packet = AudioPacket.parse(_build_opus_packet(seq, n_frames=1))
        transcripts.extend(pipeline.ingest(packet))

    # Two transcripts. "hello" appears once; " world" appears once.
    text = "".join(t.text for t in transcripts)
    assert text.count("hello") == 1
    assert "world" in text


def test_streaming_handles_silence_segment():
    """A hop where mlx-whisper returns no segments produces no transcript."""
    mlx = ScriptedMlx([
        _whisper_response_for([("hello", 0.0, 0.015)]),
        {"text": "", "segments": []},  # silence
        _whisper_response_for([("world", 0.020, 0.035)]),
    ])
    backend = WhisperStreamingBackend(mlx_transcribe=mlx)
    streamer = streaming_from_tokens(backend, sample_rate=16000, hop_ms=20, window_ms=100)
    pipeline = AudioIngestPipeline(
        reassembler=SessionReassembler(0),
        decoder=FakeOpusDecoder(),
        transcriber=streamer,
        hop_ms=20,
        window_ms=100,
        sample_rate=16000,
    )
    transcripts: list[Transcript] = []
    for seq in range(3):
        packet = AudioPacket.parse(_build_opus_packet(seq, n_frames=1))
        transcripts.extend(pipeline.ingest(packet))

    # 2 transcripts (hop 0: hello; hop 1: silent; hop 2: world).
    assert len(transcripts) == 2
    assert transcripts[0].text.strip() == "hello"
    assert transcripts[1].text.strip() == "world"
