"""Tests for the transport-agnostic gateway core.

GatewayCore is the per-connection session state machine. It takes already-deframed
inbound messages — JSON control (§E) and binary §C.6 audio — and returns a list of
outbound §E messages. It owns NO socket, so all of its behaviour is unit-testable
with a fake pipeline. The websockets adapter is a thin shell that only moves bytes.

Wire reminder: this whole plane is BLE-relayed (XIAO -> Android -> WS). WiFi is not
involved here; it is reserved for end-of-day video retrieval.
"""

import struct

import pytest

from sense_server.events.store import InMemoryEventStore
from sense_server.gateway.core import GatewayCore, GatewayError
from sense_server.ingest.audio_packet import PacketType, VadState
from sense_server.ingest.pipeline import AudioIngestPipeline
from sense_server.ingest.reassembler import SessionReassembler
from sense_server.protocol.messages import Ack, Bye, Hello, RequestChunks, TranscriptMsg


class FakeDecoder:
    def decode(self, frame: bytes) -> bytes:
        return b"\x00" * 640


class FakeTranscriber:
    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        self.calls += 1
        return f"seg{self.calls}"


def make_core(window_ms: int = 100) -> GatewayCore:
    def factory(start_seq: int) -> AudioIngestPipeline:
        return AudioIngestPipeline(
            reassembler=SessionReassembler(start_seq=start_seq),
            decoder=FakeDecoder(),
            transcriber=FakeTranscriber(),
            window_ms=window_ms,
            sample_rate=16000,
        )

    return GatewayCore(pipeline_factory=factory)


def audio_bytes(chunk_seq: int, n_frames: int, vad: int = VadState.SPEECH) -> bytes:
    frames = [bytes([chunk_seq & 0xFF])] * n_frames
    header = struct.pack(
        "<BIIBBB",
        (1 << 4) | PacketType.MEMORY_CHUNK,
        chunk_seq,
        chunk_seq * 20,
        vad,
        len(frames),
        0,
    )
    return header + b"".join(struct.pack("<B", len(f)) + f for f in frames)


def test_hello_binds_session_and_acks_the_start_cursor():
    core = make_core()

    out = core.on_control(Hello(session_id="s1", start_seq=10))

    assert out == [Ack(session_id="s1", next_seq=10)]


def test_full_window_of_audio_yields_transcript_then_ack():
    core = make_core(window_ms=100)  # 5 frames per window
    core.on_control(Hello(session_id="s1", start_seq=0))

    out = core.on_audio(audio_bytes(0, n_frames=5))

    assert out == [
        TranscriptMsg(session_id="s1", text="seg1", duration_ms=100),
        Ack(session_id="s1", next_seq=1),
    ]


def test_gap_triggers_backfill_request_and_ack_holds_at_gap_head():
    core = make_core(window_ms=100)
    core.on_control(Hello(session_id="s1", start_seq=0))

    out = core.on_audio(audio_bytes(3, n_frames=5))  # seq 0..2 missing

    # no transcript (audio is buffered behind the gap), but a backfill + held ack
    assert out == [
        RequestChunks(session_id="s1", start=0, end=3),
        Ack(session_id="s1", next_seq=0),
    ]


def test_bye_flushes_buffered_subwindow_audio_into_a_transcript():
    core = make_core(window_ms=100)  # needs 5 frames for a full window
    core.on_control(Hello(session_id="s1", start_seq=0))
    assert core.on_audio(audio_bytes(0, n_frames=3)) == [
        Ack(session_id="s1", next_seq=1)
    ]  # 3 frames buffered, no full window -> just an ack

    out = core.on_control(Bye(session_id="s1"))

    assert out == [TranscriptMsg(session_id="s1", text="seg1", duration_ms=60)]


def test_audio_before_hello_is_a_protocol_violation():
    core = make_core()
    with pytest.raises(GatewayError):
        core.on_audio(audio_bytes(0, n_frames=5))


# ---- persistence: transcripts become durable §F capture events ----------------


def make_core_with_store(window_ms: int = 100):
    store = InMemoryEventStore()

    def factory(start_seq: int) -> AudioIngestPipeline:
        return AudioIngestPipeline(
            reassembler=SessionReassembler(start_seq=start_seq),
            decoder=FakeDecoder(),
            transcriber=FakeTranscriber(),
            window_ms=window_ms,
            sample_rate=16000,
        )

    return GatewayCore(pipeline_factory=factory, event_store=store), store


def test_transcripts_are_persisted_as_ordered_capture_events():
    core, store = make_core_with_store(window_ms=100)  # 5 frames/window
    core.on_control(Hello(session_id="s1", start_seq=0))

    core.on_audio(audio_bytes(0, n_frames=12))  # 2 full windows + 2 leftover

    events = store.events("s1")
    assert [(e.seq, e.start_ms, e.duration_ms, e.kind) for e in events] == [
        (0, 0, 100, "transcript"),
        (1, 100, 100, "transcript"),
    ]
    assert [e.event_id for e in events] == ["s1:0", "s1:1"]


def test_bye_flushed_transcript_is_persisted():
    core, store = make_core_with_store(window_ms=100)
    core.on_control(Hello(session_id="s1", start_seq=0))
    core.on_audio(audio_bytes(0, n_frames=3))  # buffered, no full window

    core.on_control(Bye(session_id="s1"))

    events = store.events("s1")
    assert len(events) == 1
    assert events[0].seq == 0
    assert events[0].duration_ms == 60  # 3 frames * 20 ms


def test_replaying_a_session_does_not_duplicate_events():
    core, store = make_core_with_store(window_ms=100)
    for _ in range(2):  # same session captured twice (at-least-once replay)
        core.on_control(Hello(session_id="s1", start_seq=0))
        core.on_audio(audio_bytes(0, n_frames=5))
        core.on_control(Bye(session_id="s1"))

    assert len(store.events("s1")) == 1  # deduped by event_id
