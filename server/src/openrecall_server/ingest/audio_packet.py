"""§C.6 audio packet — the wire format the wearable emits over the audio channel.

Layout (little-endian), per the V1 interface specification:

    byte 0      ver(hi4) / ptype(lo4)
    bytes 1-4   chunk_seq      (uint32)
    bytes 5-8   rel_ts_ms      (uint32)  device ms of first frame
    byte 9      vad_state
    byte 10     frame_count
    byte 11     flags          bit0 historical, bit1 last-of-request
    byte 12..   repeated: [u8 len][opus bytes] x frame_count

The packet carries one or more Opus frames. Durability is provided by the
device ring buffer + resync, not this packet, so parsing is strict: a malformed
packet is rejected rather than partially accepted.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

_HEADER = struct.Struct("<BIIBBB")  # ver/ptype, chunk_seq, rel_ts_ms, vad, count, flags
_HEADER_LEN = _HEADER.size


class PacketType(IntEnum):
    LIVE = 0
    MEMORY_CHUNK = 1
    HISTORICAL = 2


class VadState(IntEnum):
    GAP_MARKER = 0  # suppressed silence span; not a dropout
    SPEECH = 1
    PREROLL = 2
    HANGOVER = 3


class AudioPacketError(ValueError):
    """Raised when a byte buffer is not a well-formed §C.6 audio packet."""


@dataclass(frozen=True, slots=True)
class AudioPacket:
    version: int
    ptype: PacketType
    chunk_seq: int
    rel_ts_ms: int
    vad_state: VadState
    flags: int
    frames: list[bytes]

    @property
    def is_silence_gap(self) -> bool:
        """True for a VAD-suppressed silence span (must NOT be treated as a dropout)."""
        return self.vad_state is VadState.GAP_MARKER

    @property
    def is_last_of_request(self) -> bool:
        """True on the final packet of a request_buffer (retrospective) replay."""
        return bool(self.flags & 0b10)

    def encode(self) -> bytes:
        """Serialise to §C.6 wire bytes — the canonical inverse of :meth:`parse`.

        This is what the device (and its reference simulator) emits; the firmware
        mirrors this exact layout in C. Frame lengths and counts must fit in a byte.
        """
        if len(self.frames) > 0xFF:
            raise AudioPacketError(f"too many frames: {len(self.frames)} > 255")
        header = _HEADER.pack(
            ((self.version & 0x0F) << 4) | (int(self.ptype) & 0x0F),
            self.chunk_seq,
            self.rel_ts_ms,
            int(self.vad_state),
            len(self.frames),
            self.flags,
        )
        body = bytearray()
        for i, frame in enumerate(self.frames):
            if len(frame) > 0xFF:
                raise AudioPacketError(f"frame {i} too long: {len(frame)} > 255 bytes")
            body.append(len(frame))
            body.extend(frame)
        return header + bytes(body)

    @classmethod
    def parse(cls, data: bytes) -> AudioPacket:
        if len(data) < _HEADER_LEN:
            raise AudioPacketError(
                f"buffer too short for header: {len(data)} < {_HEADER_LEN}"
            )

        ver_ptype, chunk_seq, rel_ts_ms, vad, frame_count, flags = _HEADER.unpack_from(data)
        version = (ver_ptype >> 4) & 0x0F
        ptype_raw = ver_ptype & 0x0F

        try:
            ptype = PacketType(ptype_raw)
        except ValueError as exc:
            raise AudioPacketError(f"unknown packet type {ptype_raw}") from exc
        try:
            vad_state = VadState(vad)
        except ValueError as exc:
            raise AudioPacketError(f"unknown vad_state {vad}") from exc

        frames: list[bytes] = []
        offset = _HEADER_LEN
        for i in range(frame_count):
            if offset >= len(data):
                raise AudioPacketError(
                    f"expected {frame_count} frames, ran out of data at frame {i}"
                )
            frame_len = data[offset]
            offset += 1
            end = offset + frame_len
            if end > len(data):
                raise AudioPacketError(
                    f"frame {i} truncated: need {frame_len} bytes, "
                    f"have {len(data) - offset}"
                )
            frames.append(data[offset:end])
            offset = end

        return cls(
            version=version,
            ptype=ptype,
            chunk_seq=chunk_seq,
            rel_ts_ms=rel_ts_ms,
            vad_state=vad_state,
            flags=flags,
            frames=frames,
        )
