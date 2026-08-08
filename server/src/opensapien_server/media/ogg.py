"""Ogg Opus muxing — on read only (spec D4, §3.2).

This module never runs on the ingest path. That is the whole point: RFC 7845
headers, page checksums and granule bookkeeping are fiddly, and a bug in them
on the write path destroys audio permanently and silently. Here, the frame log
is already safe on disk and this is a pure function over it — a bug means
delete the cache, fix the code, serve again.

References: RFC 3533 (Ogg), RFC 7845 (Ogg Opus). Granule positions are counted
in 48 kHz samples regardless of the actual sample rate, which is why 20 ms is
960 granules even though the stream is 16 kHz.
"""
from __future__ import annotations

import struct
from typing import Iterable, Iterator

OPUS_SAMPLE_RATE = 16000
# RFC 7845 §4: granule positions are always in 48 kHz units.
GRANULES_PER_FRAME = 960  # 20 ms at 48 kHz
PRE_SKIP = 312  # the encoder delay Opus decoders are told to discard

# A single 20 ms silent frame. Opus TOC byte 0xF8 selects SILK/hybrid... in
# practice this is the canonical minimal "comfort noise / silence" packet the
# reference encoder emits for a fully silent 20 ms mono frame at 16 kHz.
SILENT_FRAME = b"\xf8\xff\xfe"

_PAGE_HEADER = struct.Struct("<4sBBqIIIB")

_CRC_TABLE: list[int] = []


def _crc_table() -> list[int]:
    """Ogg's CRC-32: polynomial 0x04C11DB7, no reflection, no final xor.

    Deliberately not :func:`zlib.crc32`, which uses a reflected algorithm and
    a final complement — it produces a different value and every page would
    be rejected.
    """
    if _CRC_TABLE:
        return _CRC_TABLE
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if r & 0x80000000 else (r << 1) & 0xFFFFFFFF
        _CRC_TABLE.append(r)
    return _CRC_TABLE


def _crc32(data: bytes) -> int:
    table = _crc_table()
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[((crc >> 24) & 0xFF) ^ byte]
    return crc


def _segment_table(packet_sizes: Iterable[int]) -> bytes:
    """Lacing values for a page: each packet as 255-byte runs plus a remainder.

    A packet whose length is an exact multiple of 255 needs a trailing zero
    lacing value, or the decoder reads it as continuing into the next page.
    """
    out = bytearray()
    for size in packet_sizes:
        while size >= 255:
            out.append(255)
            size -= 255
        out.append(size)
    return bytes(out)


def _page(
    *,
    packets: list[bytes],
    granule: int,
    serial: int,
    sequence: int,
    header_type: int,
) -> bytes:
    segments = _segment_table(len(p) for p in packets)
    header = _PAGE_HEADER.pack(
        b"OggS", 0, header_type, granule, serial, sequence, 0, len(segments),
    )
    body = header + segments + b"".join(packets)
    crc = _crc32(body)
    # Splice the checksum in over the zero placeholder at bytes 22..26.
    return body[:22] + struct.pack("<I", crc) + body[26:]


def _id_header(*, channels: int = 1, sample_rate: int = OPUS_SAMPLE_RATE) -> bytes:
    return (
        b"OpusHead"
        + bytes([1, channels])
        + struct.pack("<H", PRE_SKIP)
        + struct.pack("<I", sample_rate)
        + struct.pack("<h", 0)   # output gain
        + bytes([0])             # channel mapping family 0
    )


def _comment_header(vendor: bytes = b"opensapien") -> bytes:
    return (
        b"OpusTags"
        + struct.pack("<I", len(vendor))
        + vendor
        + struct.pack("<I", 0)   # zero user comments
    )


def mux(
    frames: Iterable[bytes],
    *,
    serial: int = 1,
    frames_per_page: int = 50,
) -> Iterator[bytes]:
    """Wrap raw Opus frames in an Ogg Opus container, yielding pages.

    An empty frame (a gap slot from the frame log) becomes
    :data:`SILENT_FRAME`. Substituting rather than skipping is what keeps the
    D5 invariant visible to the *player*: the audio a user scrubs must be as
    long as the transcript says it is, or the playhead and the transcript
    disagree.

    ``frames_per_page`` is 50 — one page per second of audio. A page can hold
    255 lacing runs, so this leaves headroom, and a shorter page means less
    audio lost to a truncated final page.
    """
    yield _page(
        packets=[_id_header()], granule=0, serial=serial, sequence=0, header_type=0x02,
    )
    yield _page(
        packets=[_comment_header()], granule=0, serial=serial, sequence=1, header_type=0,
    )

    sequence = 2
    granule = 0
    batch: list[bytes] = []
    for frame in frames:
        batch.append(frame if frame else SILENT_FRAME)
        if len(batch) >= frames_per_page:
            granule += GRANULES_PER_FRAME * len(batch)
            yield _page(
                packets=batch, granule=granule, serial=serial,
                sequence=sequence, header_type=0,
            )
            sequence += 1
            batch = []
    # The final page must carry the end-of-stream flag, or a player treats
    # the file as truncated and may refuse to report a duration.
    granule += GRANULES_PER_FRAME * len(batch)
    yield _page(
        packets=batch, granule=granule, serial=serial,
        sequence=sequence, header_type=0x04,
    )


def mux_to_bytes(frames: Iterable[bytes], **kwargs) -> bytes:
    return b"".join(mux(frames, **kwargs))
