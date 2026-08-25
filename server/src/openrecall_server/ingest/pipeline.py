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
from typing import TYPE_CHECKING, Callable

from .audio_packet import AudioPacket
from .reassembler import SessionReassembler
from .sentence_coalescer import DEFAULT_PAUSE_MS, SentenceCoalescer
from .streaming_transcriber import (
    Segment,
    StreamingTranscriber,
    streaming_from_text,
    streaming_from_tokens,
)
from .transcriber import OpusDecoder, Transcript, Transcriber

if TYPE_CHECKING:
    from ..media.audio import AudioStore
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


def _peak_of(pcm: bytes) -> int:
    """Loudest 16-bit LE sample in ``pcm``, scaled to 0-255.

    Stdlib only, no numpy: the waveform is 34 bars on a phone screen, so
    per-frame precision beyond a byte is wasted, and adding a hard numpy
    dependency to the ingest path for it would be a poor trade.
    """
    if not pcm:
        return 0
    peak = 0
    for i in range(0, len(pcm) - 1, 2):
        sample = pcm[i] | (pcm[i + 1] << 8)
        if sample >= 0x8000:
            sample -= 0x10000
        magnitude = -sample if sample < 0 else sample
        if magnitude > peak:
            peak = magnitude
    # Scale against 32767, not 32768, so a full-scale positive sample reaches
    # 255 rather than stopping one short of it.
    return min(255, peak * 255 // 32767)


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
        audio_store: "AudioStore | None" = None,
        session_id: str | None = None,
        persist_audio: bool = True,
        audio_enabled=None,
        rel_ts_sink: Callable[[str, int], None] | None = None,
        sentence_coalesce: bool = False,
        sentence_pause_ms: int = DEFAULT_PAUSE_MS,
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
        self._streamer: StreamingTranscriber | SentenceCoalescer
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
        # Sentence coalescing (off by default; enabled by the production
        # factory via OPENRECALL_SENTENCE_COALESCE). The raw streamer emits
        # one Segment per hop (~one word at a low-latency hop); the coalescer
        # accumulates those into sentence Segments so each Transcript is a
        # readable sentence, not a single word. The streamer's cursor/dedup
        # logic is left untouched — the coalescer is a transparent wrapper.
        if sentence_coalesce:
            self._streamer = SentenceCoalescer(
                self._streamer, sample_rate=sample_rate, pause_ms=sentence_pause_ms,
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
        # Audio persistence (spec §3.1). `persist_audio` defaults to on so
        # Phase 3 stands alone; the `capture.save_audio` setting overrides it
        # once Phase 4 lands.
        self._audio = audio_store
        self._audio_session = session_id
        self._persist_audio = persist_audio
        # Device ms of the first packet we saw. The device's rel_ts_ms is
        # boot-relative, so it only becomes session time once anchored.
        self._audio_anchor_ms: int | None = None
        # The `capture.audio_enabled` backstop (spec §4.1). The toggle's real
        # mechanism is a device command, but commands only reach a connected
        # device — so the server also refuses to process audio while the
        # toggle is off. Without this the switch is cosmetic in exactly the
        # case a user cares about: a device that ignores the command and
        # keeps streaming.
        self._audio_enabled = audio_enabled or (lambda: True)
        # P3 §3.3: rel_ts->session sidecar sink. Called once per persisted
        # packet so SessionTimelineIndex can track each session's min/max
        # rel_ts. Fed from the packet (not CaptureEvent) — the event has no
        # rel_ts_ms field. Wrapped in try/except in _persist so a sink
        # failure never tears the audio path.
        self._rel_ts_sink = rel_ts_sink

    def set_audio_target(self, session_id: str) -> None:
        """Bind this pipeline's audio log to a session id.

        The pipeline is constructed by a factory that only knows
        ``start_seq``; the session id arrives with ``hello``. Rather than
        thread it through every factory signature, the gateway binds it here.
        """
        self._audio_session = session_id

    def _persist(self, packet: AudioPacket) -> None:
        """Write one packet's frames at their true session-time slot (D5).

        Placement comes from the packet's own ``rel_ts_ms``, taken **before**
        reassembly. That is deliberate, and it is the one place this deviates
        from the obvious "tap the reassembler's output": the reassembler
        merges frames from several packets into one ordered list, and in doing
        so discards the per-packet timestamp that says *where on the timeline*
        those frames belong. Without it there is nothing left to distinguish
        "20 ms of speech" from "20 ms of speech after four minutes of
        silence", and the log would compact real time out of existence.

        The cost is that a late packet the reassembler would have slotted back
        into place is dropped from the log instead (``write_at`` refuses to
        rewrite history). That trade is the right way round: a rare missing
        20 ms is a click, while a shifted timeline breaks scrubbing for the
        whole recording.
        """
        if self._audio is None or not self._persist_audio or self._audio_session is None:
            return
        if self._audio_anchor_ms is None:
            self._audio_anchor_ms = packet.rel_ts_ms
        if self._rel_ts_sink is not None and self._audio_session is not None:
            try:
                self._rel_ts_sink(self._audio_session, packet.rel_ts_ms)
            except Exception:
                logger.exception("rel_ts_sink_failed session=%s", self._audio_session)
        slot = max(0, (packet.rel_ts_ms - self._audio_anchor_ms) // FRAME_MS)
        try:
            self._audio.write_at(
                self._audio_session, slot, packet.frames,
                peaks=self._peaks_of(packet.frames),
            )
        except Exception:
            # Audio persistence is a feature; transcription is the product.
            # A full disk must not take the live stream down with it.
            logger.warning(
                "audio_persist_failed session=%s slot=%d",
                self._audio_session, slot, exc_info=True,
            )

    def _enabled(self) -> bool:
        """Whether capture is currently allowed. A failing check reads as
        enabled: losing a conversation to a settings-store hiccup is worse
        than briefly capturing while a toggle is being flipped."""
        try:
            return bool(self._audio_enabled())
        except Exception:
            logger.warning("audio_enabled_check_failed", exc_info=True)
            return True

    def _peaks_of(self, frames: list[bytes]) -> list[int]:
        """Per-frame amplitude peak, 0-255, for the waveform (spec §3.3).

        Decoded here rather than reused from the transcription buffer because
        that buffer is post-reassembly and carries no slot mapping — and the
        peak file's whole value is that byte N is slot N. An extra Opus decode
        of 20 ms of mono audio is microseconds against a Whisper call.

        A decode failure yields a zero peak: a flat bar is a much better
        outcome than a failed request for the recording.
        """
        peaks: list[int] = []
        for frame in frames:
            try:
                pcm = self._decoder.decode(frame)
            except Exception:
                peaks.append(0)
                continue
            peaks.append(_peak_of(pcm))
        return peaks

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
        if not self._enabled():
            # Still fed to the reassembler above so the chunk_seq cursor keeps
            # advancing — the §E ack must stay truthful, or re-enabling the
            # toggle leaves the device backfilling a gap that no longer exists.
            # Nothing is persisted, decoded or transcribed.
            return []
        self._persist(packet)
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
        """Transcribe whatever audio remains (partial final hop at session end)
        and drain the streamer.

        Draining always runs, even when there is no leftover PCM: with
        sentence coalescing a sentence may be held pending with no audio
        left to transcribe, and it must be emitted at session end. Without
        coalescing the raw streamer returns ``[]`` when its buffer is empty,
        so this is a no-op for the legacy per-hop path.
        """
        out: list[Transcript] = []
        spk = None
        if self._pcm_buffer:
            pcm = bytes(self._pcm_buffer)
            self._pcm_buffer.clear()
            self._absolute_ms += len(pcm) * 1000 // (self._sample_rate * 2)
            segments = self._streamer.feed(pcm)
            spk = self._speaker(pcm)
            for seg in segments:
                duration = seg.end_ms - seg.start_ms
                if duration <= 0:
                    duration = len(pcm) * 1000 // (self._sample_rate * 2)
                out.append(Transcript(
                    text=seg.text, duration_ms=duration,
                    speaker=(spk.speaker_id if spk else None),
                    speaker_confidence=(spk.confidence if spk else None),
                    speaker_assignment=(spk.assignment if spk else None),
                ))
        # Drain the streamer/coalescer so a pending sentence is flushed.
        tail = self._streamer.flush()
        for seg in tail:
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
