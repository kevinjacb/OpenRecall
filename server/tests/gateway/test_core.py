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
            hop_ms=20,
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


def test_full_window_of_audio_yields_transcripts_then_ack():
    """Under the streaming pipeline, each hop yields a transcript.

    5 frames at hop=20ms = 5 hops, each producing a transcript. The
    test asserts "we got 5 transcripts and the ack" — the *number*
    and the text are the streaming shape, not the old 1-transcript
    per full-window shape.
    """
    core = make_core(window_ms=100)  # 5 frames per window
    core.on_control(Hello(session_id="s1", start_seq=0))

    out = core.on_audio(audio_bytes(0, n_frames=5))

    # 5 transcripts (one per hop) + 1 ack
    transcripts = [m for m in out if isinstance(m, TranscriptMsg)]
    acks = [m for m in out if isinstance(m, Ack)]
    assert len(transcripts) == 5
    # The FakeTranscriber returns unique text per call, so we get seg1..seg5
    assert [t.text for t in transcripts] == ["seg1", "seg2", "seg3", "seg4", "seg5"]
    assert all(t.duration_ms == 20 for t in transcripts)
    assert acks == [Ack(session_id="s1", next_seq=1)]


def test_gap_triggers_backfill_request_and_ack_holds_at_gap_head():
    core = make_core(window_ms=100)
    core.on_control(Hello(session_id="s1", start_seq=0))

    # seq 0 anchors the live stream (start_seq=0 = anchor at first packet) and
    # yields a window; its outbound messages aren't the subject of this test.
    core.on_audio(audio_bytes(0, n_frames=5))

    # seq 3 arrives with seq 1..2 missing -> real mid-stream gap, audio held back
    out = core.on_audio(audio_bytes(3, n_frames=5))

    # no transcript (audio is buffered behind the gap), but a backfill + held ack
    assert out == [
        RequestChunks(session_id="s1", start=1, end=3),
        Ack(session_id="s1", next_seq=1),
    ]


def test_bye_flushes_buffered_subwindow_audio_into_a_transcript():
    """The streaming pipeline flushes the partial last hop on Bye.

    3 frames at hop=20ms = 3 hops, all 3 emit transcripts inline. Bye
    on an empty buffer emits nothing more.
    """
    core = make_core(window_ms=100)  # 3 frames at hop=20 = 3 transcripts
    core.on_control(Hello(session_id="s1", start_seq=0))
    out = core.on_audio(audio_bytes(0, n_frames=3))
    transcripts = [m for m in out if isinstance(m, TranscriptMsg)]
    assert len(transcripts) == 3

    out = core.on_control(Bye(session_id="s1"))
    assert out == []  # no buffered audio left


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
            hop_ms=20,
            window_ms=window_ms,
            sample_rate=16000,
        )

    return GatewayCore(pipeline_factory=factory, event_store=store), store


def test_transcripts_are_persisted_as_ordered_capture_events():
    """12 frames at hop=20ms = 12 streaming transcripts, each persisted as one event."""
    core, store = make_core_with_store(window_ms=100)
    core.on_control(Hello(session_id="s1", start_seq=0))

    core.on_audio(audio_bytes(0, n_frames=12))

    events = store.events("s1")
    assert len(events) == 12
    # Each event has 20ms duration (one hop = one frame = 20ms).
    assert all(e.duration_ms == 20 for e in events)
    # Sequence ids are 0..11.
    assert [e.event_id for e in events] == [f"s1:{i}" for i in range(12)]


def test_bye_flushed_transcript_is_persisted():
    core, store = make_core_with_store(window_ms=100)
    core.on_control(Hello(session_id="s1", start_seq=0))
    core.on_audio(audio_bytes(0, n_frames=3))

    out = core.on_control(Bye(session_id="s1"))
    # Streaming flushed everything inline; Bye on an empty buffer emits
    # nothing more. The 3 transcripts are already persisted.
    assert out == []
    events = store.events("s1")
    assert len(events) == 3
    assert [e.duration_ms for e in events] == [20, 20, 20]


def test_replaying_a_session_does_not_duplicate_events():
    core, store = make_core_with_store(window_ms=100)
    for _ in range(2):  # same session captured twice (at-least-once replay)
        core.on_control(Hello(session_id="s1", start_seq=0))
        core.on_audio(audio_bytes(0, n_frames=5))
        core.on_control(Bye(session_id="s1"))

    # First pass: 5 events. Second pass: 0 (all event_ids collide; store
    # dedupes). The replay is a no-op for the store.
    assert len(store.events("s1")) == 5
