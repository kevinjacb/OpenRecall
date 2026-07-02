"""Tests for §C.6 audio packet parsing (the wire format the firmware emits)."""

import struct

import pytest

from sense_server.ingest.audio_packet import (
    AudioPacket,
    PacketType,
    VadState,
    AudioPacketError,
)


def build_packet(
    *,
    ver: int = 1,
    ptype: int = PacketType.MEMORY_CHUNK,
    chunk_seq: int = 0,
    rel_ts_ms: int = 0,
    vad_state: int = VadState.SPEECH,
    flags: int = 0,
    frames: list[bytes],
) -> bytes:
    """Construct a §C.6 packet exactly as firmware lays it out (little-endian)."""
    header = struct.pack(
        "<BIIBBB",
        ((ver & 0x0F) << 4) | (ptype & 0x0F),
        chunk_seq,
        rel_ts_ms,
        vad_state,
        len(frames),
        flags,
    )
    body = b"".join(struct.pack("<B", len(f)) + f for f in frames)
    return header + body


def test_parses_memory_chunk_with_two_opus_frames():
    raw = build_packet(
        chunk_seq=412,
        rel_ts_ms=184213,
        vad_state=VadState.SPEECH,
        frames=[b"\x01\x02\x03", b"\x04\x05\x06\x07"],
    )

    pkt = AudioPacket.parse(raw)

    assert pkt.version == 1
    assert pkt.ptype is PacketType.MEMORY_CHUNK
    assert pkt.chunk_seq == 412
    assert pkt.rel_ts_ms == 184213
    assert pkt.vad_state is VadState.SPEECH
    assert pkt.frames == [b"\x01\x02\x03", b"\x04\x05\x06\x07"]


def test_rejects_packet_truncated_mid_frame():
    raw = build_packet(frames=[b"\x01\x02\x03"])
    truncated = raw[:-1]  # drop one byte of the opus payload

    with pytest.raises(AudioPacketError):
        AudioPacket.parse(truncated)


def test_gap_marker_carries_no_frames_and_is_distinguishable_from_speech():
    raw = build_packet(
        chunk_seq=500,
        rel_ts_ms=190000,
        vad_state=VadState.GAP_MARKER,
        frames=[],
    )

    pkt = AudioPacket.parse(raw)

    assert pkt.frames == []
    assert pkt.vad_state is VadState.GAP_MARKER
    assert pkt.is_silence_gap is True


def test_speech_packet_is_not_a_silence_gap():
    pkt = AudioPacket.parse(build_packet(vad_state=VadState.SPEECH, frames=[b"\x01"]))

    assert pkt.is_silence_gap is False


def test_rejects_unknown_packet_type():
    raw = build_packet(ptype=9, frames=[b"\x01"])

    with pytest.raises(AudioPacketError):
        AudioPacket.parse(raw)


def test_encode_round_trips_through_parse():
    pkt = AudioPacket(
        version=1,
        ptype=PacketType.MEMORY_CHUNK,
        chunk_seq=412,
        rel_ts_ms=184213,
        vad_state=VadState.SPEECH,
        flags=0b10,
        frames=[b"\x01\x02\x03", b"\x04\x05\x06\x07"],
    )

    assert AudioPacket.parse(pkt.encode()) == pkt


def test_encode_matches_the_firmware_byte_layout():
    pkt = AudioPacket(
        version=1,
        ptype=PacketType.MEMORY_CHUNK,
        chunk_seq=412,
        rel_ts_ms=184213,
        vad_state=VadState.SPEECH,
        flags=0,
        frames=[b"\x01\x02\x03", b"\x04\x05\x06\x07"],
    )

    assert pkt.encode() == build_packet(
        chunk_seq=412,
        rel_ts_ms=184213,
        vad_state=VadState.SPEECH,
        frames=[b"\x01\x02\x03", b"\x04\x05\x06\x07"],
    )


def test_encode_rejects_a_frame_longer_than_255_bytes():
    pkt = AudioPacket(
        version=1,
        ptype=PacketType.MEMORY_CHUNK,
        chunk_seq=0,
        rel_ts_ms=0,
        vad_state=VadState.SPEECH,
        flags=0,
        frames=[b"\x00" * 256],  # length must fit in one byte
    )

    with pytest.raises(AudioPacketError):
        pkt.encode()
