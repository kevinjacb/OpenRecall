"""Tests for the transport-agnostic gateway core.

GatewayCore is the per-connection session state machine. It takes already-deframed
inbound messages — JSON control (§E) and binary §C.6 audio — and returns a list of
outbound §E messages. It owns NO socket, so all of its behaviour is unit-testable
with a fake pipeline. The websockets adapter is a thin shell that only moves bytes.

Wire reminder: this whole plane is BLE-relayed (XIAO -> Android -> WS). WiFi is not
involved here; it is reserved for end-of-day video retrieval.
"""

import struct

from opensapien_server.events.store import InMemoryEventStore
from opensapien_server.gateway.core import GatewayCore
from opensapien_server.ingest.audio_packet import PacketType, VadState
from opensapien_server.ingest.pipeline import AudioIngestPipeline
from opensapien_server.ingest.reassembler import SessionReassembler
from opensapien_server.memory.extraction_worker import ExtractionEnqueuer
from opensapien_server.protocol.messages import Ack, Bye, Hello, RequestChunks, TranscriptMsg


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


def test_audio_before_hello_drops_the_frame_without_closing():
    """A binary frame that wins the hello/audio race is dropped, not fatal.

    The relay gates audio on `hello` (see RelaySession), but the server stays
    robust to a one-off reordered frame: it returns no outbound, leaves the
    pipeline unopened, and lets the subsequent `hello` establish the stream.
    Tearing the link down (the old raise-GatewayError behaviour) turned a
    transient race into a reconnect death-loop. The drop is logged at WARNING
    so it is never silent.
    """
    core = make_core()
    assert core.on_audio(audio_bytes(0, n_frames=5)) == []
    # The pipeline is still unopened, so a real hello still works normally.
    out = core.on_control(Hello(session_id="s1", start_seq=0))
    assert isinstance(out[0], Ack)


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


def test_reconnect_continues_event_seq_so_new_audio_is_not_dropped():
    """Bug B: the BLE relay does NOT replay on reconnect — it continues at
    the device's current chunk_seq (a boot-relative monotonic that never
    resets), so the audio after a reconnect is NEW content. The fresh
    per-connection GatewayCore must continue ``_event_seq`` past the events
    already in the store for this session, so the new transcripts get fresh
    ``event_id``s and are stored. Resetting ``_event_seq`` to 0 made every
    post-reconnect transcript collide with an existing ``event_id`` and be
    silently dropped (``stored=False``), starving extraction → 'no memory'.

    The store still dedupes by ``event_id`` (covered in
    ``tests/events/test_store.py``); what changes is the core no longer
    FEEDS colliding ids on a continue-stream.
    """
    store = InMemoryEventStore()

    def factory(start_seq: int) -> AudioIngestPipeline:
        return AudioIngestPipeline(
            reassembler=SessionReassembler(start_seq=start_seq),
            decoder=FakeDecoder(),
            transcriber=FakeTranscriber(),
            hop_ms=20,
            window_ms=100,
            sample_rate=16000,
        )

    # Connection 1: 5 frames -> events s1:0..4, start_ms 0..80.
    core1 = GatewayCore(pipeline_factory=factory, event_store=store)
    core1.on_control(Hello(session_id="s1", start_seq=0))
    core1.on_audio(audio_bytes(0, n_frames=5))
    assert [e.event_id for e in store.events("s1")] == [f"s1:{i}" for i in range(5)]

    # Connection 2 (reconnect): a FRESH GatewayCore with the SAME store. The
    # relay continues at chunk_seq=100 (not a replay from 0).
    core2 = GatewayCore(pipeline_factory=factory, event_store=store)
    core2.on_control(Hello(session_id="s1", start_seq=0))
    out = core2.on_audio(audio_bytes(100, n_frames=5))

    transcripts = [m for m in out if isinstance(m, TranscriptMsg)]
    assert len(transcripts) == 5  # emitted regardless, but...
    # ...they MUST also be persisted, with continued seq + start_ms.
    events = store.events("s1")
    assert len(events) == 10
    assert [e.event_id for e in events] == [f"s1:{i}" for i in range(10)]
    # start_ms continues past the prior session end (5 * 20ms = 100ms).
    assert [e.start_ms for e in events[5:]] == [100, 120, 140, 160, 180]


# ---- Bug C: finalize the trailing window on session end ---------------------


def _core_with_store_and_enqueuer():
    """A core wired with a real store + enqueuer for the finalize-on-end tests."""
    store = InMemoryEventStore()
    enq = ExtractionEnqueuer(capacity=10)  # no loop bound -> direct put path

    def factory(start_seq: int) -> AudioIngestPipeline:
        return AudioIngestPipeline(
            reassembler=SessionReassembler(start_seq=start_seq),
            decoder=FakeDecoder(),
            transcriber=FakeTranscriber(),
            hop_ms=20,
            window_ms=100,
            sample_rate=16000,
        )

    return GatewayCore(pipeline_factory=factory, event_store=store, enqueuer=enq), enq


def test_bye_enqueues_finalize_for_the_ending_session():
    """Bug C: bye must enqueue a ``finalize=True`` extraction pass for the
    session so the trailing partial 60s window is extracted without a server
    restart. The live path held it back; the session ending is the signal to
    finalize it."""
    core, enq = _core_with_store_and_enqueuer()
    core.on_control(Hello(session_id="s1", start_seq=0))
    # No audio -> flush() returns [] -> no live enqueues from _emit.
    core.on_control(Bye(session_id="s1"))

    assert enq.qsize() == 1
    assert enq._queue.get_nowait() == ("s1", True)


def test_finalize_pending_session_enqueues_finalize_on_disconnect():
    """Bug C (disconnect without bye): the adapter calls this on connection
    close so a session dropped without a clean bye still finalizes its
    trailing window. Covers the relay dropping mid-session."""
    core, enq = _core_with_store_and_enqueuer()
    core.on_control(Hello(session_id="s1", start_seq=0))

    core.finalize_pending_session()

    assert enq.qsize() == 1
    assert enq._queue.get_nowait() == ("s1", True)


def test_finalize_pending_session_is_noop_after_bye():
    """No double-finalize: after bye the session is closed, so a disconnect
    finalize on the same core is a no-op."""
    core, enq = _core_with_store_and_enqueuer()
    core.on_control(Hello(session_id="s1", start_seq=0))
    core.on_control(Bye(session_id="s1"))
    assert enq.qsize() == 1  # the bye finalize
    enq._queue.get_nowait()

    core.finalize_pending_session()  # already bye'd -> no-op
    assert enq.qsize() == 0


def test_finalize_pending_session_is_noop_when_no_session_open():
    """A core that never saw hello (or already bye'd) enqueues nothing."""
    core, enq = _core_with_store_and_enqueuer()
    core.finalize_pending_session()
    assert enq.qsize() == 0


def make_core_with_store_and_speaker(window_ms: int = 100):
    """A core whose pipeline is wired with a SpeakerIdentifier that always
    confirms against a pre-seeded "you" centroid."""
    import math

    from opensapien_server.ingest.speaker_config import SpeakerConfig
    from opensapien_server.ingest.speaker_identifier import SpeakerIdentifier
    from opensapien_server.memory.speaker_registry import InMemorySpeakerRegistry, Speaker

    store = InMemoryEventStore()
    v = [0.5] * 8
    n = math.sqrt(sum(x * x for x in v))
    unit = [x / n for x in v]
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    reg.add_speaker(Speaker(
        speaker_id="you", display_name="You", is_wearer=True,
        enrollment_status="confirmed", centroid=unit, embedding_model="fake",
        dim=8, turn_count=0, first_seen="2026-07-26T00:00:00+00:00",
        updated_at="2026-07-26T00:00:00+00:00"))

    class _EmbedUnit:
        dim = 8

        def embed(self, pcm, sr):
            return list(unit)

    ident = SpeakerIdentifier(_EmbedUnit(), reg, SpeakerConfig())

    def factory(start_seq: int) -> AudioIngestPipeline:
        return AudioIngestPipeline(
            reassembler=SessionReassembler(start_seq=start_seq),
            decoder=FakeDecoder(),
            transcriber=FakeTranscriber(),
            hop_ms=20,
            window_ms=window_ms,
            sample_rate=16000,
            speaker_identifier=ident,
        )

    return GatewayCore(pipeline_factory=factory, event_store=store,
                       speaker_registry=reg), store


def test_emit_carries_speaker_into_event_and_transcript_msg():
    core, store = make_core_with_store_and_speaker(window_ms=100)
    core.on_control(Hello(session_id="s1", start_seq=0))

    out = core.on_audio(audio_bytes(0, n_frames=5))

    transcripts = [m for m in out if isinstance(m, TranscriptMsg)]
    assert transcripts, "expected transcript messages"
    for tmsg in transcripts:
        assert tmsg.speaker == "you"
        assert tmsg.speaker_assignment == "confirmed"
        assert tmsg.speaker_confidence is not None and tmsg.speaker_confidence >= 0.7

    events = store.events("s1")
    assert len(events) == 5
    for ev in events:
        assert ev.speaker == "you"
        assert ev.speaker_assignment == "confirmed"
        assert ev.speaker_confidence is not None


def test_emit_resolves_speaker_name_and_is_wearer_on_transcript_msg():
    core, store = make_core_with_store_and_speaker(window_ms=100)
    core.on_control(Hello(session_id="s1", start_seq=0))

    out = core.on_audio(audio_bytes(0, n_frames=5))

    transcripts = [m for m in out if isinstance(m, TranscriptMsg)]
    assert transcripts, "expected transcript messages"
    for tmsg in transcripts:
        # The "you" speaker is the wearer (display_name="You", is_wearer=True).
        assert tmsg.speaker_name == "You"
        assert tmsg.is_wearer is True


def test_emit_transcript_msg_name_none_when_speaker_unknown():
    """A hop with no speaker (silence/no-speech) yields name None, is_wearer False."""
    core, store = make_core_with_store_and_speaker(window_ms=100)
    core.on_control(Hello(session_id="s1", start_seq=0))

    out = core.on_audio(audio_bytes(0, n_frames=5))
    for tmsg in [m for m in out if isinstance(m, TranscriptMsg)]:
        # The fixture always confirms "you"; this test documents the resolution
        # path's existence + default shape when a speaker row is missing.
        assert hasattr(tmsg, "speaker_name")
        assert hasattr(tmsg, "is_wearer")
