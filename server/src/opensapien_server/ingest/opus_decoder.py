"""Real Opus stream decoder (libopus via ``opuslib``).

Implements the :class:`~opensapien_server.ingest.transcriber.OpusDecoder` protocol. An
Opus decoder is *stateful* (inter-frame prediction), so one instance belongs to one
session/stream. Kept out of the tested core's import path: ``opuslib`` is imported
lazily at construction.

Install the extra and the native lib on the Mac:

    brew install opus
    pip install -e '.[opus]'
"""

from __future__ import annotations


class OpusStreamDecoder:
    def __init__(self, sample_rate: int = 16000, channels: int = 1, frame_ms: int = 20) -> None:
        import opuslib

        self._decoder = opuslib.Decoder(sample_rate, channels)
        self._frame_size = sample_rate * frame_ms // 1000  # samples per channel per frame

    def decode(self, frame: bytes) -> bytes:
        """Decode one Opus frame to 16-bit LE mono PCM bytes."""
        return self._decoder.decode(frame, self._frame_size)
