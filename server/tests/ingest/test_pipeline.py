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

from sense_server.ingest.audio_packet import AudioPacket, PacketType, VadState
from sense_server.ingest.pipeline import AudioIngestPipeline
from sense_server.ingest.reassembler import SessionReassembler


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
    """Records each window it is asked to transcribe; returns a deterministic text."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []  # (pcm_len, sample_rate) per call

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        self.calls.append((len(pcm), sample_rate))
        return f"seg{len(self.calls)}"


def make_pipeline(window_ms: int = 100):
    dec = FakeDecoder()
    tr = FakeTranscriber()
    pipe = AudioIngestPipeline(
        reassembler=SessionReassembler(start_seq=0),
        decoder=dec,
        transcriber=tr,
        window_ms=window_ms,
        sample_rate=16000,
    )
    return pipe, dec, tr


def test_emits_a_transcript_once_a_full_window_of_audio_is_buffered():
    # window_ms=100 -> 5 frames (20 ms each) per window.
    pipe, dec, tr = make_pipeline(window_ms=100)

    out = pipe.ingest(pkt(0, n_frames=5))

    assert len(out) == 1
    assert out[0].text == "seg1"
    assert out[0].duration_ms == 100
    assert dec.frames_decoded == 5
    # transcriber got 5 frames * 640 bytes of PCM at 16 kHz
    assert tr.calls == [(5 * FakeDecoder.BYTES_PER_FRAME, 16000)]


def test_sub_window_audio_is_buffered_and_not_transcribed_until_flush():
    pipe, dec, tr = make_pipeline(window_ms=100)  # needs 5 frames

    out = pipe.ingest(pkt(0, n_frames=3))  # only 3 frames -> no full window

    assert out == []
    assert tr.calls == []

    flushed = pipe.flush()
    assert len(flushed) == 1
    assert flushed[0].text == "seg1"
    assert flushed[0].duration_ms == 60  # 3 frames * 20 ms


def test_flush_on_empty_buffer_emits_nothing():
    pipe, _dec, tr = make_pipeline()
    assert pipe.flush() == []
    assert tr.calls == []


def test_one_ingest_can_emit_multiple_windows_and_keep_the_remainder():
    pipe, dec, tr = make_pipeline(window_ms=100)  # 5 frames per window

    out = pipe.ingest(pkt(0, n_frames=12))  # 2 full windows + 2 leftover

    assert [t.text for t in out] == ["seg1", "seg2"]
    assert all(t.duration_ms == 100 for t in out)
    assert len(tr.calls) == 2

    # the 2 leftover frames stay buffered until flush
    tail = pipe.flush()
    assert len(tail) == 1
    assert tail[0].duration_ms == 40  # 2 frames * 20 ms


def test_pipeline_exposes_cursor_and_missing_range_for_backfill():
    pipe, _dec, _tr = make_pipeline(window_ms=100)

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
    pipe, dec, tr = make_pipeline(window_ms=100)  # 5 frames per window

    # seq 0 anchors the live stream (start_seq=0 means "anchor at first packet")
    # and contributes a full window immediately.
    out0 = pipe.ingest(pkt(0, n_frames=5))
    assert [t.text for t in out0] == ["seg1"]
    assert dec.frames_decoded == 5

    # seq 2 arrives before seq 1 -> reassembler holds it, no audio decoded
    held = pipe.ingest(pkt(2, n_frames=5))
    assert held == []
    assert dec.frames_decoded == 5  # unchanged
    assert pipe.missing_range() == (1, 2)

    # seq 1 arrives -> its 5 frames + buffered seq 2's 5 frames = 10 -> 2 windows
    out = pipe.ingest(pkt(1, n_frames=5))
    assert [t.text for t in out] == ["seg2", "seg3"]
    assert dec.frames_decoded == 15
