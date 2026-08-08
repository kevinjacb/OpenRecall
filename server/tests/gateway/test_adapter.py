"""Tests for the websocket adapter's message router.

The router is the only unit-testable part of the transport shell: it maps an
inbound frame (text JSON control, or binary §C.6 audio) to a GatewayCore call and
serialises the outbound §E messages to JSON strings ready for ``ws.send``. The
async socket loop around it does nothing but move bytes.
"""

import json
import struct

from openrecall_server.gateway.adapter import handle_message
from openrecall_server.gateway.core import GatewayCore
from openrecall_server.ingest.audio_packet import PacketType, VadState
from openrecall_server.ingest.pipeline import AudioIngestPipeline
from openrecall_server.ingest.reassembler import SessionReassembler


class FakeDecoder:
    def decode(self, frame: bytes) -> bytes:
        return b"\x00" * 640


class FakeTranscriber:
    """Returns a distinct string per call so each hop emits a new segment.

    A constant string would be collapsed by the streaming transcriber's
    cross-hop short-phrase dedup (the phantom-repeat defense) — which is
    covered in test_streaming.py, not here. Here we want one transcript per
    hop to verify the routing/plumbing contract.
    """

    def __init__(self) -> None:
        self._n = 0

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        self._n += 1
        return f"hello {self._n}"


def make_core(window_ms: int = 100) -> GatewayCore:
    def factory(start_seq: int) -> AudioIngestPipeline:
        return AudioIngestPipeline(
            reassembler=SessionReassembler(start_seq=start_seq),
            decoder=FakeDecoder(),
            transcriber=FakeTranscriber(),
            hop_ms=20,
            window_ms=window_ms,
            sample_rate=16000,
        )

    return GatewayCore(pipeline_factory=factory)


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


def test_text_frame_is_routed_to_control_and_replies_are_json_strings():
    core = make_core()

    replies = handle_message(core, '{"type": "hello", "session_id": "s1", "start_seq": 0}')

    assert [json.loads(r) for r in replies] == [
        {"type": "ack", "session_id": "s1", "next_seq": 0}
    ]


def test_binary_frame_is_routed_to_audio():
    """Under the streaming pipeline, 5 frames @ hop=20ms = 5 transcripts.

    The str-returning FakeTranscriber returns a distinct string each call,
    so each hop produces a new committed segment (the str adapter's start_ms
    is the trailing edge of an ever-growing buffer, advancing past the
    committed cursor every hop).
    """
    core = make_core(window_ms=100)
    handle_message(core, '{"type": "hello", "session_id": "s1", "start_seq": 0}')

    replies = handle_message(core, audio_bytes(0, n_frames=5))

    parsed = [json.loads(r) for r in replies]
    transcripts = [m for m in parsed if m["type"] == "transcript"]
    acks = [m for m in parsed if m["type"] == "ack"]
    assert len(transcripts) == 5
    assert [t["text"] for t in transcripts] == [f"hello {i}" for i in range(1, 6)]
    assert all(t["duration_ms"] == 20 for t in transcripts)
    assert acks == [{"type": "ack", "session_id": "s1", "next_seq": 1}]


def test_build_pipeline_factory_threads_all_whisper_config_fields():
    """build_pipeline_factory reads every WhisperConfig field at call time
    (not deferred into the mlx-needing factory closure), so a typo in an
    attribute name raises immediately. This pins the threading and guards
    against silent default-fallback on a renamed field."""
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory

    cfg = load_agent_config({
        "OPENRECALL_WHISPER_NO_SPEECH_THRESHOLD": "0.7",
        "OPENRECALL_WHISPER_LOGPROB_THRESHOLD": "-0.8",
        "OPENRECALL_WHISPER_COMPRESSION_RATIO_THRESHOLD": "3.0",
        "OPENRECALL_WHISPER_CONDITION_ON_PREVIOUS_TEXT": "true",
        "OPENRECALL_WHISPER_HALLUCINATION_BLOCKLIST_ENABLED": "false",
        "OPENRECALL_WHISPER_HALLUCINATION_MAX_WORDS": "6",
        "OPENRECALL_WHISPER_HALLUCINATION_PHRASES": "thank you, hello",
        "OPENRECALL_WHISPER_VAD_MODE": "webrtc",
        "OPENRECALL_WHISPER_VAD_AGGRESSIVENESS": "2",
    })
    # Returns a factory closure; building it must not raise (all attr names
    # are valid). It must NOT call the mlx/opus lazy imports.
    factory = build_pipeline_factory(whisper_config=cfg.whisper)
    assert callable(factory)

    # No-config path (whisper_config=None) also yields a factory using defaults.
    assert callable(build_pipeline_factory(whisper_config=None))
