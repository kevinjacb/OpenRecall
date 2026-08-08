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
from typing import TYPE_CHECKING

from .audio_packet import AudioPacket
from .reassembler import SessionReassembler
from .streaming_transcriber import (
    Segment,
    StreamingTranscriber,
    streaming_from_text,
    streaming_from_tokens,
)
from .transcriber import OpusDecoder, Transcript, Transcriber

if TYPE_CHECKING:
    from .speaker_identifier import SpeakerAssignment, SpeakerIdentifier

FRAME_MS = 20  # one Opus frame == 20 ms of audio (V1 audio spec)
DEFAULT_HOP_MS = 1000  # streaming hop size (one Whisper call per second)
DEFAULT_WINDOW_MS = 5000  # streaming context (5s of rolling audio)
# Speaker-ID cadence is decoupled from the transcription hop: the embedder is
# fed a rolling window this wide (not the hop slice). Resemblyzer needs >= ~1.6s
# for a stable embedding (ResemblyzerSpeakerEmbedder._WARMUP_MS = 1600), but the
# transcription hop is 1s for responsiveness — feeding the hop slice to the
# embedder made every embed() return None (1000 < 1600), so every Transcript
# carried speaker=None and no speaker was ever minted. 2s comfortably clears the
# 1.6s floor while keeping the hop at 1s.
DEFAULT_SPEAKER_WINDOW_MS = 2000

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
        speaker_identifier: "SpeakerIdentifier | None" = None,
        speaker_window_ms: int = DEFAULT_SPEAKER_WINDOW_MS,
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
        self._identifier = speaker_identifier  # optional; None when disabled
        # Rolling speaker-ID window, decoupled from the transcription hop. The
        # embedder is fed the last `speaker_window_ms` of PCM on every hop, not
        # the 1s hop slice, so it always sees >= ~1.6s (Resemblyzer's floor).
        # Only used when a speaker identifier is wired; stays empty otherwise.
        self._speaker_window: bytearray = bytearray()
        self._speaker_window_bytes = speaker_window_ms * (sample_rate * 2 // 1000)

    def _speaker(self, pcm: bytes) -> "SpeakerAssignment | None":
        """Identify the speaker of the rolling window ending at this hop.

        Feeds the embedder the last ``speaker_window_ms`` of PCM (extended by
        this hop's slice), not the 1s hop slice itself — Resemblyzer needs
        >= ~1.6s for a stable embedding, which a 1s hop can never reach. The
        window grows hop-by-hop until it fills, then rolls. Speaker ID never
        blocks transcription: any embed failure is swallowed inside
        :meth:`SpeakerIdentifier.identify` and returns None, so the hop is
        still transcribed (with ``speaker=None``). Returns None while the
        identifier is disabled or the window has not yet accumulated enough
        audio for a stable embedding.
        """
        if self._identifier is None:
            return None
        self._speaker_window.extend(pcm)
        if len(self._speaker_window) > self._speaker_window_bytes:
            # Keep only the trailing window (drop the head).
            del self._speaker_window[:-self._speaker_window_bytes]
        return self._identifier.identify(bytes(self._speaker_window), self._sample_rate)

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
            spk = self._speaker(pcm)
            for seg in segments:
                # For the str adapter, seg.end_ms - seg.start_ms == 0;
                # use the segment's end_ms as the duration, falling back
                # to hop_ms if the backend reported no end time.
                duration = seg.end_ms - seg.start_ms
                if duration <= 0:
                    duration = self._streamer._hop_ms  # type: ignore[attr-defined]
                out.append(Transcript(
                    text=seg.text, duration_ms=duration,
                    speaker=(spk.speaker_id if spk else None),
                    speaker_confidence=(spk.confidence if spk else None),
                    speaker_assignment=(spk.assignment if spk else None),
                ))
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
        spk = self._speaker(pcm)
        out: list[Transcript] = []
        for seg in [*segments, *tail]:
            duration = seg.end_ms - seg.start_ms
            if duration <= 0:
                duration = len(pcm) * 1000 // (self._sample_rate * 2)
            out.append(Transcript(
                text=seg.text, duration_ms=duration,
                speaker=(spk.speaker_id if spk else None),
                speaker_confidence=(spk.confidence if spk else None),
                speaker_assignment=(spk.assignment if spk else None),
            ))
        return out
