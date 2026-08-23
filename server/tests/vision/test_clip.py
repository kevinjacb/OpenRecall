"""MJPEG splitting + keyframe selection for video clip ingestion (P4b)."""
from __future__ import annotations

import pytest

from openrecall_server.vision.clip import split_mjpeg

# A minimal JPEG: SOI (FF D8) + a body + EOI (FF D9). Real JPEGs are larger
# but split_mjpeg only scans SOI boundaries, so this is enough.
def _jpeg(marker: bytes = b"") -> bytes:
    return b"\xff\xd8" + marker + b"\xff\xd9"


def test_split_mjpeg_splits_on_soi_boundaries():
    clip = _jpeg(b"aaa") + _jpeg(b"bbb") + _jpeg(b"ccc")
    frames = split_mjpeg(clip)
    assert len(frames) == 3
    assert frames[0] == b"\xff\xd8" + b"aaa" + b"\xff\xd9"
    assert frames[1] == b"\xff\xd8" + b"bbb" + b"\xff\xd9"
    assert frames[2] == b"\xff\xd8" + b"ccc" + b"\xff\xd9"


def test_split_mjpeg_single_frame():
    frames = split_mjpeg(_jpeg(b"only"))
    assert len(frames) == 1
    assert frames[0].startswith(b"\xff\xd8")


def test_split_mjpeg_ignores_ff00_escape_in_body():
    # 0xFF00 is a stuffed byte inside JPEG scan data, NOT a marker.
    # split_mjpeg must only split on 0xFFD8 (SOI), not on 0xFF00.
    body = b"\xff\x00\xd8fake"  # an FF00 that looks like D8 if you skip the 00
    frames = split_mjpeg(_jpeg(body) + _jpeg(b"two"))
    assert len(frames) == 2


def test_split_mjpeg_raises_on_no_soi():
    with pytest.raises(ValueError):
        split_mjpeg(b"not a jpeg at all")


def test_split_mjpeg_empty_raises():
    with pytest.raises(ValueError):
        split_mjpeg(b"")