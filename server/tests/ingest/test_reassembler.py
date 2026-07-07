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


def test_live_stream_anchors_at_first_packet_when_start_seq_is_zero():
    # start_seq=0 means "live, no resume point." The device's chunk_seq is a
    # boot-relative monotonic counter that doesn't reset per session, and the
    # phone drops the first few packets before its socket is ready, so the first
    # packet the server sees is at an arbitrary chunk_seq (26170 here, mirroring
    # the real bring-up log). The reassembler anchors the stream there instead
    # of waiting for 0 (which never comes) — the fix for the silent stall where
    # every packet was buffered and no audio ever reached the transcriber.
    r = SessionReassembler(start_seq=0)

    out = r.accept(pkt(26170, [b"a", b"b"]))

    assert out == [b"a", b"b"]
    assert r.next_expected_seq == 26171
    assert r.missing_range() is None  # no gap — the stream anchored here

    # subsequent in-order packets deliver normally
    assert r.accept(pkt(26171, [b"c"])) == [b"c"]


def test_resume_with_start_seq_waits_for_the_anchor_and_tolerates_reorder():
    # start_seq > 0 means "resume from here" — the caller named the anchor, so we
    # wait for it (don't latch to the first packet) and tolerate reordering.
    r = SessionReassembler(start_seq=500)

    # seq 502 arrives first -> held (not anchored), gap at 500
    assert r.accept(pkt(502, [b"z"])) == []
    assert r.missing_range() == (500, 502)

    # seq 500 anchors; 502 stays buffered until 501 fills (501 not yet present)
    flushed = r.accept(pkt(500, [b"a"]))
    assert flushed == [b"a"]
    assert r.next_expected_seq == 501
    assert r.missing_range() == (501, 502)
