"""Audio ingest pipeline: ordered packets -> PCM -> windowed transcription.

Connects the deterministic Phase 0 slice end to end:

    AudioPacket -> SessionReassembler -> OpusDecoder -> window buffer -> Transcriber

The reassembler hands us Opus frames in ``chunk_seq`` order (and nothing for
silence gaps, reordering, or duplicates). We decode each frame to PCM, accumulate
whole frames, and once a window's worth of audio (``window_ms``) is buffered we
hand that window to the transcriber and emit a :class:`Transcript`. Leftover audio
stays buffered until the next frames arrive or :meth:`flush` is called (e.g. at
end of session / utterance).

Each Opus frame is a fixed 20 ms (the V1 spec), so window/segment durations are
derived from frame counts and are independent of the decoder's output size.
"""

from __future__ import annotations

from .audio_packet import AudioPacket
from .reassembler import SessionReassembler
from .transcriber import OpusDecoder, Transcriber, Transcript

FRAME_MS = 20  # one Opus frame == 20 ms of audio (V1 audio spec)


class AudioIngestPipeline:
    def __init__(
        self,
        reassembler: SessionReassembler,
        decoder: OpusDecoder,
        transcriber: Transcriber,
        window_ms: int = 5000,
        sample_rate: int = 16000,
    ) -> None:
        if window_ms % FRAME_MS != 0:
            raise ValueError(f"window_ms must be a multiple of {FRAME_MS}")
        self._reassembler = reassembler
        self._decoder = decoder
        self._transcriber = transcriber
        self._window_frames = window_ms // FRAME_MS
        self._sample_rate = sample_rate
        self._buffer: list[bytes] = []  # decoded PCM, one entry per 20 ms frame

    @property
    def next_expected_seq(self) -> int:
        """Next contiguous chunk_seq the stream wants (drives §E ack cursor)."""
        return self._reassembler.next_expected_seq

    def missing_range(self) -> tuple[int, int] | None:
        """Contiguous head gap ``[start, end)`` to backfill, or None (drives §E request_chunks)."""
        return self._reassembler.missing_range()

    def ingest(self, packet: AudioPacket) -> list[Transcript]:
        """Ingest one packet; return any transcripts completed as a result."""
        for frame in self._reassembler.accept(packet):
            self._buffer.append(self._decoder.decode(frame))

        out: list[Transcript] = []
        while len(self._buffer) >= self._window_frames:
            out.append(self._cut(self._window_frames))
        return out

    def flush(self) -> list[Transcript]:
        """Transcribe whatever audio remains (partial final window)."""
        if not self._buffer:
            return []
        return [self._cut(len(self._buffer))]

    def _cut(self, n_frames: int) -> Transcript:
        """Pop ``n_frames`` of PCM off the front, transcribe them, emit a segment."""
        window = self._buffer[:n_frames]
        del self._buffer[:n_frames]
        pcm = b"".join(window)
        text = self._transcriber.transcribe(pcm, self._sample_rate)
        return Transcript(text=text, duration_ms=n_frames * FRAME_MS)
