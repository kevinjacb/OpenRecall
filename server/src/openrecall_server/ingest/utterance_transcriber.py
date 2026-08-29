"""Utterance-mode transcription — one backend call per utterance.

Why this exists (evidence from real device data, 2026-08-29): the rolling
window + committed-cursor streaming design re-transcribes overlapping audio
every hop and relies on the backend reporting *stable word timestamps across
calls* to deduplicate the overlap. Parakeet-TDT's token alignments shift
between calls as the window slides, so the timestamp cursor fails and
overlapping fragments are re-emitted and glued together — live output like
``"trans transcription not not correct?"`` for audio that the very same model
transcribes perfectly in one pass ("why is this transcription not correct?").

This transcriber removes the failing mechanism instead of patching it:

  * ``feed()`` accumulates PCM into an utterance buffer,
  * the end of an utterance is detected from **synthesized gap silence** —
    the pipeline inserts exact-zero PCM for firmware VAD gaps, so a trailing
    run of zeros >= ``end_silence_ms`` means the speaker stopped
    (the firmware encodes ~600 ms of real hangover audio first, so total
    detection latency is ~1.2 s after speech ends),
  * the whole utterance is transcribed **once** (the audio is never
    re-transcribed, so there is nothing to deduplicate), and one
    :class:`Segment` is emitted per backend sentence — ``sentence_id`` IS
    meaningful within a single call, so the model's own sentence structure
    is used directly, with no downstream coalescing needed.

Cost: one ASR call per utterance instead of ~4/second — roughly an order of
magnitude less compute — at the price of utterance-final latency (text
appears ~1 s after a pause instead of trickling live).

Duck-types the streamer surface the pipeline holds (``feed`` / ``flush`` /
``_hop_ms`` / ``committed_ms``), so :class:`AudioIngestPipeline` can carry it
in ``self._streamer`` unchanged.
"""
from __future__ import annotations

import logging
from array import array

from .streaming_transcriber import (
    Segment,
    StreamingBackend,
    _group_by_sentence_id,
)

logger = logging.getLogger(__name__)

# End-of-utterance: this much contiguous *exact-zero* PCM at the buffer tail.
# Only the pipeline's synthesized gap fill is exactly zero, and it starts
# after the firmware's ~600 ms encoded hangover — so this is a true
# "the wearer stopped speaking" signal, not an energy heuristic.
DEFAULT_END_SILENCE_MS = 560

# Do not spend an ASR call on a buffer with less audible audio than this
# (a VAD blip, or pure synthesized silence at session start).
DEFAULT_MIN_SPEECH_MS = 240

# Bound the buffer for a speaker who never pauses: transcribe and reset.
# Generous — conversational utterances are seconds, and one 20 s call is
# still cheap next to the per-hop scheme this replaces.
DEFAULT_MAX_UTTERANCE_MS = 20_000

# A sample counts as audible above this (~ -60 dBFS); mirrors the streaming
# transcriber's silence epsilon.
_AUDIBLE_EPSILON = 32


def _trailing_zero_bytes(pcm: bytes) -> int:
    """Length of the run of zero bytes at the end of ``pcm``."""
    n = len(pcm)
    i = n
    while i > 0 and pcm[i - 1] == 0:
        i -= 1
    return n - i


class UtteranceTranscriber:
    """Accumulate an utterance; transcribe it once; emit its sentences."""

    def __init__(
        self,
        backend: "StreamingBackend",
        sample_rate: int = 16000,
        hop_ms: int = 240,
        end_silence_ms: int = DEFAULT_END_SILENCE_MS,
        min_speech_ms: int = DEFAULT_MIN_SPEECH_MS,
        max_utterance_ms: int = DEFAULT_MAX_UTTERANCE_MS,
    ) -> None:
        self._backend = backend
        self._sample_rate = sample_rate
        # Duck-type surface: the pipeline reads ``_hop_ms`` for fallback
        # durations; it is the feed granularity, not a transcription cadence.
        self._hop_ms = hop_ms
        self._bytes_per_ms = sample_rate * 2 // 1000
        self._end_silence_bytes = end_silence_ms * self._bytes_per_ms
        self._min_speech_ms = min_speech_ms
        self._max_utterance_bytes = max_utterance_ms * self._bytes_per_ms
        self._buffer = bytearray()
        # Contiguous zero bytes at the tail of the buffer (tracked
        # incrementally so feed() never rescans the whole utterance).
        self._trailing_zeros = 0
        # Session time (ms of PCM fed so far) and the utterance's start
        # within it, so emitted Segments carry absolute timestamps like the
        # streaming transcriber's.
        self._fed_ms = 0
        self._utterance_start_ms = 0

    @property
    def committed_ms(self) -> int:
        # Everything before the current (open) utterance has been emitted.
        return self._utterance_start_ms

    def feed(self, pcm: bytes) -> list[Segment]:
        if not pcm:
            return []
        if not self._buffer:
            self._utterance_start_ms = self._fed_ms
        self._buffer.extend(pcm)
        self._fed_ms += len(pcm) // self._bytes_per_ms
        tz = _trailing_zero_bytes(pcm)
        if tz == len(pcm):
            self._trailing_zeros += tz
        else:
            self._trailing_zeros = tz

        if len(self._buffer) >= self._max_utterance_bytes:
            return self._close_utterance()
        if (
            self._trailing_zeros >= self._end_silence_bytes
            and len(self._buffer) > self._trailing_zeros
        ):
            return self._close_utterance()
        return []

    def flush(self) -> list[Segment]:
        """Transcribe whatever is buffered (session end)."""
        return self._close_utterance()

    # ------------------------------------------------------------------

    def _audible_ms(self, pcm: bytes) -> int:
        samples = array("h")
        samples.frombytes(pcm[: len(pcm) & ~1])
        audible = sum(
            1 for s in samples if s > _AUDIBLE_EPSILON or s < -_AUDIBLE_EPSILON
        )
        return audible * 1000 // self._sample_rate

    def _close_utterance(self) -> list[Segment]:
        pcm = bytes(self._buffer)
        start_ms = self._utterance_start_ms
        self._buffer.clear()
        self._trailing_zeros = 0
        self._utterance_start_ms = self._fed_ms
        if not pcm:
            return []
        if self._audible_ms(pcm) < self._min_speech_ms:
            return []
        tokens = self._backend.transcribe(pcm, self._sample_rate)
        if not tokens:
            return []
        segments: list[Segment] = []
        for sentence_id, group in _group_by_sentence_id(tokens):
            # Strip the leading-space tokenization marker per token (same
            # rationale as the streaming transcriber's join).
            text = " ".join(t.text.lstrip() for t in group)
            if not text.strip():
                continue
            segments.append(Segment(
                text=text,
                start_ms=start_ms + group[0].start_ms,
                end_ms=start_ms + group[-1].end_ms,
                sentence_id=sentence_id,
            ))
        logger.info(
            "utterance: %d ms -> %d sentence(s)",
            len(pcm) // self._bytes_per_ms, len(segments),
        )
        return segments
