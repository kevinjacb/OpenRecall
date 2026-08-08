"""Phase 3 — the Ogg Opus muxer (spec §3.2, D4).

The muxer runs on read only, so these tests check the container is
structurally valid rather than that any particular byte survived: a bug here
is recoverable by deleting the cache, which is the whole reason it lives on
this side of the write path.
"""
from __future__ import annotations

import struct

from opensapien_server.media.ogg import (
    GRANULES_PER_FRAME,
    SILENT_FRAME,
    _crc32,
    mux_to_bytes,
)


def _pages(data: bytes) -> list[dict]:
    """Walk an Ogg bitstream into its pages, verifying each checksum."""
    pages = []
    offset = 0
    while offset < len(data):
        assert data[offset:offset + 4] == b"OggS", "lost page sync"
        header_type = data[offset + 5]
        (granule,) = struct.unpack_from("<q", data, offset + 6)
        (serial,) = struct.unpack_from("<I", data, offset + 14)
        (sequence,) = struct.unpack_from("<I", data, offset + 18)
        (stored_crc,) = struct.unpack_from("<I", data, offset + 22)
        n_segments = data[offset + 26]
        table = data[offset + 27:offset + 27 + n_segments]
        body_len = sum(table)
        total = 27 + n_segments + body_len
        raw = data[offset:offset + total]
        zeroed = raw[:22] + b"\x00\x00\x00\x00" + raw[26:]
        assert _crc32(zeroed) == stored_crc, "page checksum mismatch"
        pages.append({
            "header_type": header_type,
            "granule": granule,
            "serial": serial,
            "sequence": sequence,
            "body": raw[27 + n_segments:],
            "lacing": list(table),
        })
        offset += total
    return pages


def test_the_stream_opens_with_the_two_mandatory_headers():
    pages = _pages(mux_to_bytes([b"aa"]))

    assert pages[0]["body"].startswith(b"OpusHead")
    assert pages[0]["header_type"] == 0x02  # beginning-of-stream
    assert pages[1]["body"].startswith(b"OpusTags")


def test_the_id_header_declares_mono_16k():
    (head,) = [p["body"] for p in _pages(mux_to_bytes([b"aa"]))[:1]]

    assert head[9] == 1  # channel count
    assert struct.unpack_from("<I", head, 12)[0] == 16000


def test_every_page_checksum_validates():
    # _pages asserts this on each page; a long stream exercises many pages.
    pages = _pages(mux_to_bytes([b"aa"] * 500))

    assert len(pages) > 10


def test_page_sequence_numbers_are_contiguous():
    pages = _pages(mux_to_bytes([b"aa"] * 200))

    assert [p["sequence"] for p in pages] == list(range(len(pages)))


def test_all_pages_share_one_serial():
    pages = _pages(mux_to_bytes([b"aa"] * 200, serial=4242))

    assert {p["serial"] for p in pages} == {4242}


def test_the_final_page_is_flagged_end_of_stream():
    """Without it a player treats the file as truncated and may refuse to
    report a duration — which breaks the scrubber."""
    pages = _pages(mux_to_bytes([b"aa"] * 120))

    assert pages[-1]["header_type"] == 0x04


def test_granule_advances_by_960_per_frame():
    """RFC 7845: granules are 48 kHz units regardless of the actual rate, so
    20 ms is 960 even though this stream is 16 kHz. Getting this wrong makes
    a player report the wrong duration and mis-seek."""
    pages = _pages(mux_to_bytes([b"aa"] * 3))

    assert pages[-1]["granule"] == 3 * GRANULES_PER_FRAME


def test_a_gap_slot_becomes_a_silent_frame_not_a_skipped_one():
    """The audio a user scrubs must be as long as the transcript says it is,
    or the playhead and the transcript disagree (D5)."""
    body = b"".join(p["body"] for p in _pages(mux_to_bytes([b"aa", b"", b"bb"]))[2:])

    assert SILENT_FRAME in body
    assert _pages(mux_to_bytes([b"aa", b"", b"bb"]))[-1]["granule"] == 3 * GRANULES_PER_FRAME


def test_lacing_handles_a_packet_that_is_a_multiple_of_255():
    """A 255-byte packet needs a trailing zero lacing value, or the decoder
    reads it as continuing into the next page."""
    pages = _pages(mux_to_bytes([b"x" * 255]))

    assert pages[-1]["lacing"] == [255, 0]


def test_an_empty_frame_list_still_produces_a_valid_stream():
    pages = _pages(mux_to_bytes([]))

    assert len(pages) == 3  # head, tags, and an empty end-of-stream page
    assert pages[-1]["header_type"] == 0x04


def test_ogg_crc_is_not_zlib_crc():
    """Ogg uses an unreflected CRC-32 with no final xor. zlib's differs, and
    every page would be rejected by a real decoder."""
    import zlib

    assert _crc32(b"OggS test") != zlib.crc32(b"OggS test")
