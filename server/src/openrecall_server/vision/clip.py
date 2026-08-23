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