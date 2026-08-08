"""Device-side Opus encoder for the reference simulator (mirror of the server decoder).

Encoding is the wearable's job (the firmware does this in C with the same params from
Spike 1: 16 kHz mono, 24 kbps, 20 ms, complexity 1). Kept out of the tested core's
import path — ``opuslib`` is imported lazily. Install with ``pip install -e '.[opus]'``
(plus ``brew install opus``).
"""

from __future__ import annotations


class OpusStreamEncoder:
    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        frame_ms: int = 20,
        bitrate: int = 24000,
    ) -> None:
        import opuslib
        from opuslib.exceptions import OpusError

        self._encoder = opuslib.Encoder(sample_rate, channels, opuslib.APPLICATION_VOIP)
        # Best-effort: some opuslib builds have a broken SET_BITRATE ctl binding. The
        # default VOIP bitrate is fine for the simulator, and the wire format is
        # identical, so a failure here must not take down the demo.
        try:
            self._encoder.bitrate = bitrate
        except OpusError:
            self.bitrate_set = False
        else:
            self.bitrate_set = True
        self._frame_size = sample_rate * frame_ms // 1000  # samples per channel per frame

    def encode(self, pcm_frame: bytes) -> bytes:
        """Encode one 20 ms PCM frame (16-bit LE mono) to an Opus frame."""
        return self._encoder.encode(pcm_frame, self._frame_size)
