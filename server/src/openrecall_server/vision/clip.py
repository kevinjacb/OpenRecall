"""Video clip ingestion helpers (P4b).

:func:`split_mjpeg` splits an MJPEG stream into JPEG frames by SOI boundaries
(pure-Python, no image deps). :func:`select_keyframes` picks scene-change
keyframes via Pillow (lazy-imported so this module imports without the
``video`` extra). The video upload route composes the two.
"""
from __future__ import annotations

_SOI = b"\xff\xd8"


def split_mjpeg(data: bytes) -> list[bytes]:
    """Split an MJPEG stream into its JPEG frames by SOI (``0xFFD8``) markers.

    Each frame begins at a SOI and ends just before the next SOI (or EOF).
    Splitting on SOI (not EOI) is robust: JPEG scan data stuffs ``0xFF`` as
    ``0xFF00``, so an EOI scan would mis-fire on stuffed bytes; SOI is the
    unambiguous frame-start marker. Raises ``ValueError`` if no SOI is found
    (empty or non-JPEG body — the upload route maps this to 400).
    """
    starts = []
    i = data.find(_SOI)
    while i != -1:
        starts.append(i)
        i = data.find(_SOI, i + 2)
    if not starts:
        raise ValueError("no JPEG SOI marker found — not an MJPEG stream")
    frames: list[bytes] = []
    for k in range(len(starts) - 1):
        frames.append(data[starts[k]:starts[k + 1]])
    frames.append(data[starts[-1]:])
    return frames


def _thumb(image: bytes, size: int):
    """Decode a JPEG to a ``size``x``size`` grayscale thumbnail; return pixel
    values as a flat ``list[int]`` in 0..255. Pillow is imported here so the
    module imports without the ``video`` extra."""
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(image)).convert("L").resize((size, size))
    return list(img.getdata())


def _mad(a: list[int], b: list[int]) -> float:
    """Normalized mean-absolute-difference between two thumbnails (0..1)."""
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    total = sum(abs(a[i] - b[i]) for i in range(n))
    return total / (n * 255.0)


def select_keyframes(
    frames: list[bytes],
    *,
    threshold: float,
    min_gap: int,
    cap: int,
    thumb_size: int,
) -> list[int]:
    """Return indices of frames to caption (scene-change keyframes, P4b).

    Frame 0 is always selected (the opening scene). A later frame is selected
    when its grayscale-thumb MAD vs the **last selected keyframe** exceeds
    ``threshold`` (robust to slow drift) and ≥ ``min_gap`` frames have passed
    since the last selection (cooldown — motion-heavy footage doesn't fire
    every frame). Stops once ``cap`` selections are made. No "always select
    last frame" rule: the last *scene* is already represented by the keyframe
    at its onset (the cut into it); forcing the last frame would double-count
    a no-change clip (see spec V2 + exit criterion 7). Pillow is required.
    """
    if not frames:
        return []
    selected = [0]
    last_thumb = _thumb(frames[0], thumb_size)
    for i in range(1, len(frames)):
        if len(selected) >= cap:
            break
        if i - selected[-1] < min_gap:
            continue
        t = _thumb(frames[i], thumb_size)
        if _mad(t, last_thumb) > threshold:
            selected.append(i)
            last_thumb = t
    return selected
