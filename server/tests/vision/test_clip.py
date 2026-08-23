"""MJPEG splitting + keyframe selection for video clip ingestion (P4b)."""
from __future__ import annotations

import pytest

from openrecall_server.vision.clip import split_mjpeg, select_keyframes

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


# --- select_keyframes (needs Pillow) ----------------------------------------

def _solid_jpeg(color: int) -> bytes:
    """A real 4x4 solid-color JPEG via Pillow; skip if Pillow absent."""
    pytest.importorskip("PIL")
    from PIL import Image
    import io
    img = Image.new("L", (4, 4), color)  # grayscale, solid color
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_select_keyframes_no_change_yields_one_keyframe():
    frames = [_solid_jpeg(128) for _ in range(10)]  # identical frames
    idx = select_keyframes(frames, threshold=0.12, min_gap=2, cap=12, thumb_size=4)
    assert idx == [0]


def test_select_keyframes_detects_scene_cuts():
    # 3 red, 3 white, 3 black -> cuts at 0, 3, 6
    frames = [_solid_jpeg(60), _solid_jpeg(60), _solid_jpeg(60),
              _solid_jpeg(200), _solid_jpeg(200), _solid_jpeg(200),
              _solid_jpeg(0), _solid_jpeg(0), _solid_jpeg(0)]
    idx = select_keyframes(frames, threshold=0.12, min_gap=2, cap=12, thumb_size=4)
    assert 0 in idx and 3 in idx and 6 in idx
    assert idx[0] == 0


def test_select_keyframes_respects_min_gap_cooldown():
    # alternating colors every frame: with min_gap=3, only every 3rd can fire
    frames = [_solid_jpeg(c) for c in [0, 255, 0, 255, 0, 255, 0, 255, 0]]
    idx = select_keyframes(frames, threshold=0.05, min_gap=3, cap=12, thumb_size=4)
    assert idx[0] == 0
    # no two selections closer than min_gap
    for a, b in zip(idx, idx[1:]):
        assert b - a >= 3


def test_select_keyframes_caps_total():
    # 30 cuts; cap=4 -> at most 4 selections
    frames = [_solid_jpeg(255 if i % 2 else 0) for i in range(30)]
    idx = select_keyframes(frames, threshold=0.05, min_gap=1, cap=4, thumb_size=4)
    assert len(idx) <= 4
    assert idx[0] == 0


def test_select_keyframes_empty_frames():
    assert select_keyframes([], threshold=0.12, min_gap=2, cap=12, thumb_size=4) == []
