"""Tests for the audio ingest pipeline.

The pipeline wires the deterministic core of the Phase 0 vertical slice:

    §C.6 packets -> SessionReassembler (ordering) -> OpusDecoder (frames->PCM)
                 -> windowing -> Transcriber -> Transcript segments

Both the decoder and the transcriber are pluggable behind small protocols, so the
heavyweight real implementations (libopus + MLX-whisper) live on the Mac and the
*logic* here is tested with fakes. The real impls are exercised by the RTF
measurement script, not by this unit test.
"""

import struct

from openrecall_server.ingest.audio_packet import AudioPacket, PacketType, VadState
from openrecall_server.ingest.pipeline import AudioIngestPipeline
from openrecall_server.ingest.reassembler import SessionReassembler


def pkt(chunk_seq: int, n_frames: int, vad: int = VadState.SPEECH) -> AudioPacket:
    """A speech packet carrying ``n_frames`` opaque 1-byte 'opus' frames."""
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
    body = b"".join(struct.pack("<B", len(f)) + f for f in frames)
    return AudioPacket.parse(header + body)


class FakeDecoder:
    """Each opus frame decodes to a fixed-size PCM blob (320 samples * 2 bytes)."""

    BYTES_PER_FRAME = 640

    def __init__(self) -> None:
        self.frames_decoded = 0

    def decode(self, frame: bytes) -> bytes:
        self.frames_decoded += 1
        return b"\x00" * self.BYTES_PER_FRAME


class FakeTranscriber:
    """Returns a different token per call so each hop emits a new segment.

    With the streaming transcriber, tokens whose start_ms is past
    the committed cursor are emitted; tokens re-transcribed in the
    overlap zone (start_ms < committed) are dropped. A fake that
    returns "seg1" for every call would see only the first token
    committed (all subsequent calls re-transcribe the same audio).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []  # (pcm_len, sample_rate) per call

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        self.calls.append((len(pcm), sample_rate))
        # Return a unique token per call so the streaming wrapper
        # has something new to commit each hop.
        return f"seg{len(self.calls)}"


def make_pipeline(
    window_ms: int = 100,
    hop_ms: int = 20,
    speaker_identifier=None,
    speaker_window_ms: int = 2000,
    sentence_coalesce: bool = False,
):
    """Build a pipeline with streaming defaults scaled for the test.

    The old hard-cut pipeline used a single window. The new streaming
    pipeline uses 1-hop-per-frame (hop=20ms, window=100ms here) so the
    tests exercise the new path with the same number of frames.

    ``sentence_coalesce`` defaults to False so the per-hop tests below
    keep their existing contract; the coalesced path is exercised by
    :func:`test_sentence_coalesce_groups_hops_into_one_sentence`.
    """
    dec = FakeDecoder()
    tr = FakeTranscriber()
    pipe = AudioIngestPipeline(
        reassembler=SessionReassembler(start_seq=0),
        decoder=dec,
        transcriber=tr,
        hop_ms=hop_ms,
        window_ms=window_ms,
        sample_rate=16000,
        speaker_identifier=speaker_identifier,
        speaker_window_ms=speaker_window_ms,
        sentence_coalesce=sentence_coalesce,
    )
    return pipe, dec, tr


def test_emits_a_transcript_once_a_full_window_of_audio_is_buffered():
    # 5 frames at 20 ms each = 100 ms of audio. With hop=20 ms,
    # window=100 ms, the pipeline emits one transcript per hop
    # (5 total). The transcriber fakes "seg1" for every call, so
    # the streaming wrapper sees the same token re-transcribed and
    # commits it once.
    pipe, dec, tr = make_pipeline(window_ms=100, hop_ms=20)

    out = pipe.ingest(pkt(0, n_frames=5))

    # One Transcript per hop (5 frames / 1 frame per hop = 5 hops).
    assert len(out) == 5
    assert dec.frames_decoded == 5
    # The streaming wrapper calls the backend 5 times; each call gets
    # an increasingly-long buffer. We don't assert on the exact text
    # here — that's the streaming transcriber's job, covered in
    # tests/ingest/test_streaming.py.
    assert len(tr.calls) == 5
    # Every transcriber call receives a 16 kHz PCM buffer.
    for size, sr in tr.calls:
        assert sr == 16000
        assert size > 0


def test_sub_window_audio_is_buffered_and_not_transcribed_until_flush():
    pipe, dec, tr = make_pipeline(window_ms=100, hop_ms=20)

    out = pipe.ingest(pkt(0, n_frames=3))  # only 3 frames -> 3 hops

    assert len(out) == 3
    assert len(tr.calls) == 3

    # No leftover — 3 frames / 1 frame per hop = exactly 3 hops.
    assert pipe.flush() == []


def test_flush_on_empty_buffer_emits_nothing():
    pipe, _dec, tr = make_pipeline()
    assert pipe.flush() == []
    assert tr.calls == []


def test_one_ingest_can_emit_multiple_windows_and_keep_the_remainder():
    pipe, dec, tr = make_pipeline(window_ms=100, hop_ms=20)

    out = pipe.ingest(pkt(0, n_frames=12))  # 12 frames -> 12 hops

    # 12 frames / 1 frame per hop = 12 hops, each producing 1 transcript.
    assert len(out) == 12
    assert len(tr.calls) == 12

    # no remainder
    tail = pipe.flush()
    assert tail == []


def test_pipeline_exposes_cursor_and_missing_range_for_backfill():
    pipe, _dec, _tr = make_pipeline(window_ms=100, hop_ms=20)

    assert pipe.next_expected_seq == 0
    assert pipe.missing_range() is None

    pipe.ingest(pkt(0, n_frames=1))
    assert pipe.next_expected_seq == 1
    assert pipe.missing_range() is None

    # a hole: seq 1 missing, seq 3 arrives -> backfill [1, 3)
    pipe.ingest(pkt(3, n_frames=1))
    assert pipe.next_expected_seq == 1
    assert pipe.missing_range() == (1, 3)


def test_out_of_order_packets_contribute_no_audio_until_the_gap_fills():
    pipe, dec, tr = make_pipeline(window_ms=100, hop_ms=20)

    # seq 0 anchors the live stream and produces 5 hops (5 frames).
    out0 = pipe.ingest(pkt(0, n_frames=5))
    assert len(out0) == 5
    assert dec.frames_decoded == 5

    # seq 2 arrives before seq 1 -> reassembler holds it, no audio decoded
    held = pipe.ingest(pkt(2, n_frames=5))
    assert held == []
    assert dec.frames_decoded == 5  # unchanged
    assert pipe.missing_range() == (1, 2)

    # seq 1 arrives -> its 5 frames + buffered seq 2's 5 frames -> 10 hops
    out = pipe.ingest(pkt(1, n_frames=5))
    assert len(out) == 10
    assert dec.frames_decoded == 15


def test_pipeline_threads_speaker_assignment_onto_transcripts():
    import math

    from openrecall_server.ingest.speaker_config import SpeakerConfig
    from openrecall_server.ingest.speaker_identifier import SpeakerIdentifier
    from openrecall_server.memory.speaker_registry import InMemorySpeakerRegistry, Speaker

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
    pipe, _dec, _tr = make_pipeline(speaker_identifier=ident)

    out = pipe.ingest(pkt(0, n_frames=5))  # 5 hops

    assert out, "expected transcripts from the 5 hops"
    for t in out:
        assert t.speaker == "you"
        assert t.speaker_assignment == "confirmed"
        assert t.speaker_confidence is not None and t.speaker_confidence >= 0.7


def test_pipeline_without_identifier_leaves_speaker_none():
    pipe, _dec, _tr = make_pipeline()  # no speaker_identifier

    out = pipe.ingest(pkt(0, n_frames=5))

    assert out
    for t in out:
        assert t.speaker is None
        assert t.speaker_confidence is None
        assert t.speaker_assignment is None


def test_sentence_coalesce_groups_hops_into_one_sentence():
    """With sentence coalescing on, the per-hop word Segments the raw
    streamer emits are held and flushed as a single joined sentence,
    instead of one Transcript per hop. At hop=20ms the inter-hop gap
    (20ms) is below the 400ms pause threshold and the fake transcriber
    adds no punctuation, so nothing emits mid-stream; the sentence comes
    out on flush.
    """
    pipe, _dec, _tr = make_pipeline(window_ms=100, hop_ms=20, sentence_coalesce=True)

    out = pipe.ingest(pkt(0, n_frames=5))  # 5 hops

    # All held — no sentence boundary mid-stream.
    assert out == []
    tail = pipe.flush()
    assert len(tail) == 1
    # The five per-hop words joined into one sentence.
    assert tail[0].text.split() == ["seg1", "seg2", "seg3", "seg4", "seg5"]


def test_flush_tail_sentence_keeps_last_identified_speaker():
    """Regression (commit 2b35acb): with sentence coalescing ON, the coalescer
    holds per-hop words and emits them as one sentence on flush(). The flush()
    rewrite stamped those tail sentences with the speaker of the trailing
    partial PCM (``spk``), which is ``None`` whenever the hop buffer was
    already drained — dropping the label the held hops had identified. On
    hardware this surfaced as "speaker labels missing again" because the
    final sentence of every BLE session hit this path on _on_bye/disconnect.

    The fix tracks the last identified speaker across hops
    (``self._last_speaker``) and attributes the flushed tail sentence to it.
    """
    import math
    from openrecall_server.ingest.speaker_config import SpeakerConfig
    from openrecall_server.ingest.speaker_identifier import SpeakerIdentifier
    from openrecall_server.memory.speaker_registry import (
        InMemorySpeakerRegistry,
        Speaker,
    )

    v = [0.5] * 8
    n = math.sqrt(sum(x * x for x in v))
    unit = [x / n for x in v]
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    reg.add_speaker(
        Speaker(
            speaker_id="you", display_name="You", is_wearer=True,
            enrollment_status="confirmed", centroid=unit, embedding_model="fake",
            dim=8, turn_count=0, first_seen="2026-07-26T00:00:00+00:00",
            updated_at="2026-07-26T00:00:00+00:00",
        )
    )

    class _EmbedUnit:
        dim = 8

        def embed(self, pcm, sr):
            return list(unit)

    ident = SpeakerIdentifier(_EmbedUnit(), reg, SpeakerConfig())
    pipe, _dec, _tr = make_pipeline(
        speaker_identifier=ident, sentence_coalesce=True)

    # Five hops, all held by the coalescer (no sentence boundary mid-stream).
    out = pipe.ingest(pkt(0, n_frames=5))
    assert out == []

    tail = pipe.flush()
    assert len(tail) == 1
    # The tail sentence carries the speaker identified across the held hops,
    # not None (the bug) — labels survive to the Android render gate.
    assert tail[0].speaker == "you"
    assert tail[0].speaker_assignment == "confirmed"
    assert tail[0].speaker_confidence is not None


def test_sentence_coalesce_off_emits_per_hop():
    """The default (coalesce off) keeps the one-Transcript-per-hop
    contract — the regression pin for the coalescer not silently
    changing the raw pipeline's behaviour.
    """
    pipe, _dec, _tr = make_pipeline(window_ms=100, hop_ms=20, sentence_coalesce=False)

    out = pipe.ingest(pkt(0, n_frames=5))

    assert len(out) == 5
    assert pipe.flush() == []


def test_rolling_speaker_window_decouples_embedder_from_hop():
    """The embedder is fed a rolling window, not the hop slice, so a
    min-length embedder (mirroring Resemblyzer's >=1.6s floor) still
    produces a speaker even when the hop is far below its minimum.

    With hop=20ms and a 60ms embedder floor, the hop slice (20ms) could
    never embed — but the rolling window grows hop-by-hop until it
    clears the floor, then keeps rolling. This is the regression pin for
    the bug where run_gateway's 1s hop < Resemblyzer's 1.6s floor made
    every Transcript carry speaker=None.
    """
    import math

    from openrecall_server.ingest.speaker_config import SpeakerConfig
    from openrecall_server.ingest.speaker_identifier import SpeakerIdentifier
    from openrecall_server.memory.speaker_registry import InMemorySpeakerRegistry, Speaker

    v = [0.5] * 8
    n = math.sqrt(sum(x * x for x in v))
    unit = [x / n for x in v]
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    reg.add_speaker(Speaker(
        speaker_id="you", display_name="You", is_wearer=True,
        enrollment_status="confirmed", centroid=unit, embedding_model="fake",
        dim=8, turn_count=0, first_seen="2026-07-26T00:00:00+00:00",
        updated_at="2026-07-26T00:00:00+00:00"))

    # Embedder that refuses too-short audio, exactly like Resemblyzer's
    # _WARMUP_MS gate (speaker_embedder.embed returns None below the floor).
    class _MinLenEmbed:
        dim = 8

        def __init__(self, min_bytes: int) -> None:
            self.min_bytes = min_bytes

        def embed(self, pcm, sr):
            if len(pcm) < self.min_bytes:
                return None
            return list(unit)

    # 60ms floor in bytes (16000 Hz * 2 bytes * 60ms / 1000). Hop is 20ms,
    # so the hop slice alone (640 bytes = 20ms) can never clear it.
    min_bytes = 16000 * 2 * 60 // 1000
    ident = SpeakerIdentifier(_MinLenEmbed(min_bytes), reg, SpeakerConfig())
    # 80ms rolling window: grows 20->40->60->80 then rolls at 80.
    pipe, _dec, _tr = make_pipeline(
        hop_ms=20, window_ms=100, speaker_identifier=ident, speaker_window_ms=80,
    )

    out = pipe.ingest(pkt(0, n_frames=5))  # 5 hops of 20ms each

    assert len(out) == 5, "one transcript per 20ms hop"
    speakers = [t.speaker for t in out]
    # Hops 1-2: window (20ms, 40ms) below the 60ms floor -> no speaker.
    assert speakers[0] is None, "20ms window below floor -> speaker=None"
    assert speakers[1] is None, "40ms window below floor -> speaker=None"
    # Hops 3-5: window (60ms, 80ms, 80ms rolled) at/above floor -> "you".
    assert speakers[2] == "you", "60ms window clears floor -> speaker"
    assert speakers[3] == "you", "80ms window clears floor -> speaker"
    assert speakers[4] == "you", "rolled 80ms window clears floor -> speaker"
