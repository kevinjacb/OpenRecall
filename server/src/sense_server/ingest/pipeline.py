"""Audio ingest pipeline: ordered packets -> PCM -> streaming transcription.

Connects the deterministic Phase 0 slice end to end:

    AudioPacket -> SessionReassembler -> OpusDecoder -> PCM buffer
                                          -> StreamingTranscriber

The streaming transcriber eliminates boundary-loss artifacts:
instead of cutting audio into hard 5s windows and feeding each to
Whisper independently (which loses speech that straddles the cut),
we feed 1s hops with 5s of context and use Whisper's token-level
timestamps to emit only the text that's been confirmed by an
overlapping window.

The reassembler hands us Opus frames in ``chunk_seq`` order (and nothing for
silence gaps, reordering, or duplicates). We decode each frame to PCM,
accumulate whole frames, and once a hop's worth of audio
(``hop_ms``) is buffered we hand it to the streaming transcriber and
emit any newly-committed :class:`TranscriptionSegment`s.

Each Opus frame is a fixed 20 ms (the V1 spec), so hop/window
durations are derived from frame counts and are independent of the
decoder's output size.
"""

from __future__ import annotations

import logging

from .audio_packet import AudioPacket
from .reassembler import SessionReassembler
from .streaming_transcriber import (
    Segment,
    StreamingTranscriber,
    streaming_from_text,
    streaming_from_tokens,
)
from .transcriber import OpusDecoder, Transcript, Transcriber

FRAME_MS = 20  # one Opus frame == 20 ms of audio (V1 audio spec)
DEFAULT_HOP_MS = 1000  # streaming hop size (one Whisper call per second)
DEFAULT_WINDOW_MS = 5000  # streaming context (5s of rolling audio)

logger = logging.getLogger(__name__)


class AudioIngestPipeline:
    def __init__(
        self,
        reassembler: SessionReassembler,
        decoder: OpusDecoder,
        transcriber: Transcriber | StreamingTranscriber,
        hop_ms: int = DEFAULT_HOP_MS,
        window_ms: int = DEFAULT_WINDOW_MS,
        sample_rate: int = 16000,
    ) -> None:
        if window_ms % FRAME_MS != 0:
            raise ValueError(f"window_ms must be a multiple of {FRAME_MS}")
        if hop_ms % FRAME_MS != 0:
            raise ValueError(f"hop_ms must be a multiple of {FRAME_MS}")
        if hop_ms > window_ms:
            raise ValueError(
                f"hop_ms ({hop_ms}) must be <= window_ms ({window_ms})"
            )
        self._reassembler = reassembler
        self._decoder = decoder
        self._sample_rate = sample_rate
        self._hop_frames = hop_ms // FRAME_MS
        self._streamer: StreamingTranscriber
        if isinstance(transcriber, StreamingTranscriber):
            self._streamer = transcriber
        elif isinstance(transcriber, Transcriber):
            # Legacy str-returning transcriber; wrap via the text factory.
            self._streamer = streaming_from_text(
                transcriber, sample_rate, hop_ms, window_ms,
            )
        else:
            # Anything else (duck-typed, no isinstance hit) is treated
            # as a token-returning backend — the streaming factory
            # takes its word_timestamps output at face value.
            self._streamer = streaming_from_tokens(
                transcriber,  # type: ignore[arg-type]
                sample_rate, hop_ms, window_ms,
            )
        self._pcm_buffer: bytearray = bytearray()  # decoded PCM, appended as frames arrive
        self._absolute_ms: int = 0  # total ms of audio fed to the streamer

    @property
    def next_expected_seq(self) -> int:
        """Next contiguous chunk_seq the stream wants (drives §E ack cursor)."""
        return self._reassembler.next_expected_seq

    def missing_range(self) -> tuple[int, int] | None:
        """Contiguous head gap ``[start, end)`` to backfill, or None (drives §E request_chunks)."""
        return self._reassembler.missing_range()

    def ingest(self, packet: AudioPacket) -> list[Transcript]:
        """Ingest one packet; return any transcripts completed as a result."""
        delivered = self._reassembler.accept(packet)
        for frame in delivered:
            self._pcm_buffer.extend(self._decoder.decode(frame))
        if delivered:
            logger.info(
                "pipeline: +%d frame(s) decoded -> %d bytes PCM buffered",
                len(delivered), len(self._pcm_buffer),
            )

        out: list[Transcript] = []
        hop_bytes = self._hop_frames * (self._sample_rate * FRAME_MS // 1000) * 2
        # Emit one Transcript per hop while we have at least one hop
        # of buffered audio. Each Transcript's duration_ms is the hop
        # size (the chunk of audio we just fed the streamer). The
        # streamer's segments carry word-level timestamps when the
        # backend supports them; for the str-returning fallback the
        # duration is naturally 0 in the segment, so we use hop_ms.
        while len(self._pcm_buffer) >= hop_bytes:
            pcm = bytes(self._pcm_buffer[:hop_bytes])
            del self._pcm_buffer[:hop_bytes]
            self._absolute_ms += self._streamer._hop_ms  # type: ignore[attr-defined]
            segments = self._streamer.feed(pcm)
            for seg in segments:
                # For the str adapter, seg.end_ms - seg.start_ms == 0;
                # use the segment's end_ms as the duration, falling back
                # to hop_ms if the backend reported no end time.
                duration = seg.end_ms - seg.start_ms
                if duration <= 0:
                    duration = self._streamer._hop_ms  # type: ignore[attr-defined]
                out.append(Transcript(text=seg.text, duration_ms=duration))
        return out

    def flush(self) -> list[Transcript]:
        """Transcribe whatever audio remains (partial final hop at session end)."""
        if not self._pcm_buffer:
            return []
        pcm = bytes(self._pcm_buffer)
        self._pcm_buffer.clear()
        self._absolute_ms += len(pcm) * 1000 // (self._sample_rate * 2)
        segments = self._streamer.feed(pcm)
        tail = self._streamer.flush()
        out: list[Transcript] = []
        for seg in [*segments, *tail]:
            duration = seg.end_ms - seg.start_ms
            if duration <= 0:
                duration = len(pcm) * 1000 // (self._sample_rate * 2)
            out.append(Transcript(text=seg.text, duration_ms=duration))
        return out
