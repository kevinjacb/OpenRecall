"""Tests for the per-session audio reassembler.

Responsibilities (Phase 0 slice):
  - deliver Opus frames to the transcriber in chunk_seq order
  - tolerate reordering from the BLE/relay path (buffer, then flush)
  - drop duplicates/old packets idempotently
  - treat a VAD gap-marker as an advance, not audio and not a dropout
  - surface a *real* missing range so the gateway can request backfill (§E)
"""

from sense_server.ingest.audio_packet import AudioPacket, PacketType, VadState
from sense_server.ingest.reassembler import SessionReassembler

import struct


def pkt(chunk_seq: int, frames: list[bytes], vad: int = VadState.SPEECH) -> AudioPacket:
    header = struct.pack(
        "<BIIBBB",
        (1 << 4) | PacketType.MEMORY_CHUNK,
        chunk_seq,
        chunk_seq * 20,  # rel_ts_ms stand-in
        vad,
        len(frames),
        0,
    )
    body = b"".join(struct.pack("<B", len(f)) + f for f in frames)
    return AudioPacket.parse(header + body)


def test_in_order_packets_deliver_frames_in_order():
    r = SessionReassembler(start_seq=0)

    out0 = r.accept(pkt(0, [b"a", b"b"]))
    out1 = r.accept(pkt(1, [b"c"]))

    assert out0 == [b"a", b"b"]
    assert out1 == [b"c"]
    assert r.next_expected_seq == 2
    assert r.missing_range() is None


def test_out_of_order_packet_is_buffered_then_flushed_when_gap_fills():
    r = SessionReassembler(start_seq=0)
    r.accept(pkt(0, [b"x"]))  # next_expected now == 1

    # seq 2 arrives before seq 1 -> must be held back
    held = r.accept(pkt(2, [b"z"]))
    assert held == []
    assert r.missing_range() == (1, 2)  # seq 1 is the head gap

    # seq 1 arrives -> both 1 and the buffered 2 flush in order
    flushed = r.accept(pkt(1, [b"y"]))
    assert flushed == [b"y", b"z"]
    assert r.next_expected_seq == 3
    assert r.missing_range() is None


def test_duplicate_and_old_packets_are_ignored():
    r = SessionReassembler(start_seq=0)
    r.accept(pkt(0, [b"a"]))
    r.accept(pkt(1, [b"b"]))

    redelivered = r.accept(pkt(0, [b"a"]))  # already past this seq

    assert redelivered == []
    assert r.next_expected_seq == 2


def test_silence_gap_marker_advances_without_audio_and_is_not_a_dropout():
    r = SessionReassembler(start_seq=0)

    out = r.accept(pkt(0, [], vad=VadState.GAP_MARKER))

    assert out == []  # no audio
    assert r.next_expected_seq == 1  # but the stream advanced
    assert r.missing_range() is None  # NOT a dropout


def test_real_dropout_is_reported_as_missing_range():
    r = SessionReassembler(start_seq=0)
    r.accept(pkt(0, [b"a"]))

    # seq 1 and 2 never arrive; seq 3 shows up
    r.accept(pkt(3, [b"d"]))

    assert r.missing_range() == (1, 3)  # request backfill for [1, 3)
