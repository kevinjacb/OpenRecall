"""Streaming transcriber — eliminates boundary-loss artifacts.

The hard-cut pipeline (5s windows, one Whisper call per window) loses
speech at every hop boundary: a word that straddles two windows is
transcribed twice or garbled. This module wraps any
:class:`Transcriber` (real or fake) with a streaming algorithm:

  * feed audio in 1-second hops with 5 seconds of rolling context,
  * call the underlying transcriber with the current 5s window,
  * track which tokens have been "committed" (i.e. past the audio
    that will be re-fed in the next hop), and only emit those,
  * keep the uncommitted tail in memory so a word that started at
    the boundary can finish being transcribed before we emit it.

The result: each word appears in exactly one segment, regardless of
where it falls in the hop grid.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .transcriber import Transcriber

logger = logging.getLogger(__name__)

# Cross-hop dedup: Whisper hallucinating the same short phrase on consecutive
# noise hops emits it at a NEW absolute time each hop (past the committed
# cursor), so the overlap-zone dedup does not catch it — the transcript fills
# with "Thank you. Thank you. Thank you." Collapse a segment that is identical
# to the last EMITTED segment AND short (<= this many words). Long phrases are
# left alone (real repeats are rare; the repetition filter handles true loops).
#
# Re-calibrated for Parakeet word tokens: a single-word segment repeating across
# hops is far more likely a genuine repeat (the speaker said the word twice)
# than a phantom — Parakeet's transducer does not hallucinate isolated words the
# way Whisper does. So the cross-hop dedup only fires for 2.._DEDUP_MAX_WORDS
# (multi-word phrases like "thank you" that are classic Whisper phantoms).
# Single words fall through and are emitted; the repetition filter handles true
# loops downstream.
_DEDUP_MIN_WORDS = 2
_DEDUP_MAX_WORDS = 5

# webrtcvad frame size: 20 ms at 16 kHz mono 16-bit == 640 bytes.
_VAD_FRAME_BYTES = 640


@dataclass(frozen=True, slots=True)
class Token:
    """One word with its absolute timestamps in the call's audio window.

    ``start_ms`` and ``end_ms`` are millisecond offsets into the audio
    passed to the underlying transcriber (the rolling window), not
    absolute session time. The :class:`StreamingTranscriber` translates
    to absolute time when emitting segments.

    ``sentence_id`` carries the ASR backend's sentence/segment grouping
    WITHIN one transcribe() call: tokens that belong to the same backend
    sentence share an id (> 0), so the :class:`StreamingTranscriber` can
    emit one Segment per backend sentence per call. The id is call-local
    (backends stamp the segment index, and the rolling window renumbers
    segments every hop), so it must never be compared across calls. ``0``
    is the sentinel for "no sentence structure" — the str-returning
    adapter and the flattened ``AlignedResult.tokens`` fallback both use
    0, which collapses a call's tokens into one Segment.
    """

    text: str
    start_ms: int
    end_ms: int
    sentence_id: int = 0


@dataclass(frozen=True, slots=True)
class Segment:
    """A committed slice of transcript with absolute session timestamps.

    ``sentence_id`` mirrors the emitting tokens' id (> 0 = a real backend
    sentence; 0 = no structure / per-hop grouping). The coalescer reads it
    to decide sentence boundaries.
    """

    text: str
    start_ms: int
    end_ms: int
    sentence_id: int = 0


@runtime_checkable
class StreamingBackend(Protocol):
    """The minimal interface the streaming wrapper needs from a backend.

    Production uses :class:`WhisperStreamingBackend` (mlx-whisper with
    word_timestamps=True). Tests use :class:`FakeWhisper`.

    This Protocol is intentionally NOT marked ``@runtime_checkable``
    because its duck-typed shape is identical to the legacy
    :class:`Transcriber` — the only difference is the return type
    (str vs list[Token]). Callers must construct the streaming
    transcriber via :func:`streaming_from_tokens` (token-returning
    backend) or :func:`streaming_from_text` (str-returning legacy
    transcriber) so the wrapping is unambiguous.
    """

    def transcribe(self, pcm: bytes, sample_rate: int) -> list[Token]:
        ...


class _TranscriberAdapter:
    """Adapts a vanilla :class:`Transcriber` (returns str) into a
    :class:`StreamingBackend` that returns tokens.

    The whole utterance is given a timestamp at the *end* of the audio
    it just transcribed (``duration_ms``). The streaming wrapper then
    sees the new token's start as past the committed cursor and emits
    it. The next hop's token, with the same start relative to the new
    buffer, is past the cursor too — and gets emitted. The dedup
    logic still catches re-transcriptions of the same audio in the
    overlap zone, because the start_ms in hop-local coordinates is
    unchanged.
    """

    def __init__(self, transcriber: Transcriber) -> None:
        self._t = transcriber

    def transcribe(self, pcm: bytes, sample_rate: int) -> list[Token]:
        text = self._t.transcribe(pcm, sample_rate)
        if not text or not text.strip():
            return []
        # The str-returning transcriber can't give internal timestamps,
        # so we report a token at the trailing edge of the call's
        # audio. The streaming wrapper's dedup uses start_ms to skip
        # re-transcriptions in the overlap zone: the next hop's
        # token has a larger start_ms (further in the session) and
        # is past the committed cursor, so it's emitted as new.
        duration_ms = max(1, (len(pcm) // 2) * 1000 // sample_rate)
        return [Token(text=text.strip(), start_ms=duration_ms, end_ms=duration_ms)]


def _normalize_segment_text(text: str) -> str:
    """Normalize a joined segment for cross-hop dedup matching: lowercase,
    strip punctuation, collapse whitespace. Robust to mlx appending different
    trailing punctuation across hops."""
    stripped = text.strip().lower().strip(".,!?;:'\"-()[]{}“”")
    return " ".join(stripped.split())


def _word_count(text: str) -> int:
    return len(text.split())


def _group_by_sentence_id(tokens: list[Token]) -> list[tuple[int, list[Token]]]:
    """Group consecutive tokens sharing the same ``sentence_id`` into runs.

    The backend returns tokens in audio order, and sentence ids are
    non-decreasing within a single transcription call, so consecutive tokens
    with the same id form one backend sentence. Returns ``(sentence_id,
    [tokens])`` pairs in order. Tokens with id 0 (no structure) collapse into
    one run — preserving the one-Segment-per-hop behavior for backends that
    don't expose sentence boundaries.
    """
    if not tokens:
        return []
    runs: list[tuple[int, list[Token]]] = []
    cur_id = tokens[0].sentence_id
    cur: list[Token] = []
    for tok in tokens:
        if tok.sentence_id != cur_id:
            runs.append((cur_id, cur))
            cur_id = tok.sentence_id
            cur = []
        cur.append(tok)
    if cur:
        runs.append((cur_id, cur))
    return runs


class StreamingTranscriber:
    """Wraps a :class:`StreamingBackend` to produce stable segments.

    The wrapper keeps a rolling PCM buffer of size ``window_ms`` and a
    "committed token cursor" — the millisecond offset in session
    time past which all tokens are stable. Each ``feed`` call:

      1. Appends the new PCM to the rolling buffer.
      2. Trims the front so the buffer is at most ``window_ms`` long.
      3. Calls the backend with the current buffer.
      4. Determines which tokens are *new* (start >= committed cursor)
         and emits them as a :class:`Segment`.
      5. Advances the committed cursor to the end of the last new token
         (so the next hop doesn't re-emit it).
    """

    def __init__(
        self,
        backend: "StreamingBackend | Transcriber",
        sample_rate: int = 16000,
        hop_ms: int = 1000,
        window_ms: int = 5000,
        is_token_backend: bool = False,
        vad_mode: str | None = None,
        vad_aggressiveness: int = 3,
    ) -> None:
        """Construct the streaming transcriber.

        Use the :func:`streaming_from_tokens` / :func:`streaming_from_text`
        factory functions rather than the constructor directly — they
        set ``is_token_backend`` correctly so the wrapping is unambiguous.

        ``vad_mode`` (opt-in): when ``"webrtc"``, a spectral VAD
        (webrtcvad, an optional install) gates each hop BEFORE the
        Whisper call — a pure-noise hop (zero voiced frames) is skipped
        so Whisper never sees it and can't hallucinate on it. Off by
        default: webrtcvad is an optional dependency and at high
        aggressiveness can drop quiet real speech. If webrtcvad is not
        installed, the gate logs a warning and disables itself rather
        than crash.
        """
        if hop_ms <= 0 or hop_ms > window_ms:
            raise ValueError(
                f"hop_ms ({hop_ms}) must be in (0, window_ms ({window_ms})]; "
                f"otherwise the rolling window can't cover each hop"
            )
        if window_ms <= 0:
            raise ValueError(f"window_ms must be > 0")
        if vad_mode is not None and not (0 <= vad_aggressiveness <= 3):
            raise ValueError(
                f"vad_aggressiveness ({vad_aggressiveness}) must be in [0, 3]"
            )
        if is_token_backend:
            self._backend: "StreamingBackend" = backend  # type: ignore[assignment]
        else:
            self._backend = _TranscriberAdapter(backend)
        self._sample_rate = sample_rate
        self._hop_ms = hop_ms
        self._window_ms = window_ms
        self._hop_bytes = (sample_rate * hop_ms // 1000) * 2  # 16-bit LE mono
        self._window_bytes = (sample_rate * window_ms // 1000) * 2

        # Rolling PCM buffer (most-recent ``window_ms`` of audio).
        self._buffer: bytearray = bytearray()
        # Absolute session time of the front of the rolling buffer.
        self._buffer_start_ms: int = 0
        # Absolute session time past which all tokens are committed.
        self._committed_ms: int = 0
        # Tokens emitted in the current hop (so we can extend the last
        # segment with the next hop's continuation).
        self._hop_tokens: list[Token] = []
        # Cross-hop dedup: normalized text of the last EMITTED segment.
        # A new short segment matching this is a phantom repeat on noise
        # (each hop's phantom lands past the committed cursor, so the
        # overlap dedup misses it) and is dropped without advancing the
        # cursor. Updated only on emit, so the suppression chains.
        self._last_emitted_text_norm: str = ""
        # Text-merge cursor dedup (L7 fix): normalized words of the last
        # EMITTED segment. A token that straddles the committed cursor
        # (start < committed, end > committed) is a re-transcription of
        # the overlap zone; we drop it ONLY if its text was already emitted
        # (it's the same word re-transcribed). If its text is NOT in this
        # set, it's a real word the previous hop missed — emit it.
        self._last_emitted_words: set[str] = set()
        # Opt-in spectral VAD gate (None = disabled / unavailable).
        self._vad = None
        if vad_mode == "webrtc":
            try:
                import webrtcvad
                self._vad = webrtcvad.Vad(vad_aggressiveness)
            except ImportError:
                logger.warning(
                    "vad_mode='webrtc' requested but the 'webrtcvad' package "
                    "is not installed; install it (`pip install webrtcvad`) or "
                    "unset OPENRECALL_WHISPER_VAD_MODE. The VAD gate is "
                    "disabled — Whisper will run on every hop as before."
                )
                self._vad = None

    def _hop_has_voiced_frames(self, pcm: bytes) -> bool:
        """True if any 20 ms frame of ``pcm`` is voiced speech per webrtcvad.

        webrtcvad requires exact 10/20/30 ms frames at 8/16/32/48 kHz; we feed
        20 ms frames. A trailing partial frame (< 640 bytes) is dropped rather
        than crash. A hop too short to contain even one frame is treated as
        voiced (proceed) so we never skip on a truncation artifact.
        """
        if self._vad is None or len(pcm) < _VAD_FRAME_BYTES:
            return True
        for i in range(0, len(pcm) - _VAD_FRAME_BYTES + 1, _VAD_FRAME_BYTES):
            try:
                if self._vad.is_speech(bytes(pcm[i:i + _VAD_FRAME_BYTES]), self._sample_rate):
                    return True
            except Exception:
                # A single bad frame must not gate the whole hop; treat as
                # voiced so real speech is never dropped on a decoder blip.
                return True
        return False

    @property
    def committed_ms(self) -> int:
        return self._committed_ms

    def feed(self, pcm: bytes) -> list[Segment]:
        """Append a hop of audio; return any newly-committed segments.

        The caller is responsible for feeding exactly ``hop_ms`` worth
        of audio per call (except the last call at session end, which
        may be shorter). Mixing hop sizes mid-session is undefined.
        """
        if not pcm:
            return []

        # 1. Append to the rolling buffer.
        self._buffer.extend(pcm)
        # 2. Trim the front so the buffer is at most window_bytes long.
        if len(self._buffer) > self._window_bytes:
            trim = len(self._buffer) - self._window_bytes
            del self._buffer[:trim]
            # The front of the buffer is now newer; advance the start.
            self._buffer_start_ms += (trim // 2) * 1000 // self._sample_rate

        # 2.5. Optional spectral VAD gate: skip the Whisper call entirely on a
        # pure-noise hop so it can't hallucinate on silence. The buffer (above)
        # is still extended so the rolling context is preserved; we just avoid
        # the expensive, hallucination-prone decode this hop.
        if self._vad is not None and not self._hop_has_voiced_frames(pcm):
            return []

        # 3. Call the backend with the current buffer.
        tokens = self._backend.transcribe(bytes(self._buffer), self._sample_rate)

        # 4. Determine which tokens are new and translate to absolute session
        #    time. The rule is timestamp/position-aware (L7 fix for Parakeet
        #    word tokens whose start_ms jittered behind the cursor):
        #
        #    * Token entirely behind the cursor (end <= committed): drop —
        #      already committed, no new audio.
        #    * Token straddling the cursor (start < committed < end): this is
        #      the overlap zone. Drop ONLY if the token's normalized text was
        #      already in the last emitted segment (a re-transcription of the
        #      same word). Emit it if the text is new — it's a real word the
        #      previous hop missed (Parakeet's sub-word boundaries jitter
        #      differently from Whisper's segment timestamps, so a real word's
        #      retranscribed start can land just behind the cursor).
        #    * Token past the cursor (start >= committed): always emit — it's
        #      new audio, including a genuinely repeated word said twice.
        new_tokens: list[Token] = []
        for tok in tokens:
            absolute_start = self._buffer_start_ms + tok.start_ms
            absolute_end = self._buffer_start_ms + tok.end_ms
            if absolute_end <= self._committed_ms:
                # Entirely behind the cursor — already committed.
                continue
            if absolute_start < self._committed_ms:
                # Straddles the cursor (overlap zone). Drop only if this word
                # was already emitted; emit if it's new text (L7 fix).
                tok_norm = _normalize_segment_text(tok.text)
                if tok_norm and tok_norm in self._last_emitted_words:
                    continue
            new_tokens.append(Token(
                text=tok.text,
                start_ms=absolute_start,
                end_ms=absolute_end,
                sentence_id=tok.sentence_id,
            ))

        if not new_tokens:
            return []

        # 5. Emit one Segment per backend sentence. The backend tags each
        #    token with its sentence id (> 0); consecutive tokens sharing an
        #    id form one sentence (the backend returns tokens in order, and
        #    sentence ids are non-decreasing within a call). This carries the
        #    ASR model's own sentence segmentation through the committed-cursor
        #    / dedup logic, so the downstream coalescer groups by the model's
        #    boundary instead of re-deriving it with punctuation/pause
        #    heuristics. Tokens with sentence_id == 0 (str adapter, or the
        #    flattened AlignedResult.tokens fallback) all share id 0, so they
        #    collapse into a single Segment — preserving the original
        #    one-Segment-per-hop behavior for backends with no structure.
        segments: list[Segment] = []
        for sentence_id, group in _group_by_sentence_id(new_tokens):
            # mlx-whisper prefixes non-first word tokens with a leading space
            # (a tokenization marker, not content); strip it before joining so
            # the segment is single-spaced ("Hello, world!", not "Hello,  world!").
            text = " ".join(t.text.lstrip() for t in group)
            # 5.5. Cross-hop dedup: a short segment identical to the last
            # EMITTED one is a phantom repeat on noise (the overlap dedup in
            # step 4 only catches repeats at the same absolute time; a fresh
            # phantom each hop lands past the cursor). Drop it WITHOUT
            # advancing the committed cursor — the next hop's real speech
            # still self-corrects past it.
            norm = _normalize_segment_text(text)
            if (
                norm
                and norm == self._last_emitted_text_norm
                and _DEDUP_MIN_WORDS <= _word_count(text) <= _DEDUP_MAX_WORDS
            ):
                continue
            segment = Segment(
                text=text,
                start_ms=group[0].start_ms,
                end_ms=group[-1].end_ms,
                sentence_id=sentence_id,
            )
            # 6. Advance the committed cursor to the end of this sentence's
            #    last token. We trust the backend's timestamps — a
            #    token-returning backend (Whisper with word_timestamps, or
            #    Parakeet word tokens) reports real word boundaries, and the
            #    str-returning adapter reports the trailing edge of the
            #    call's audio, which is naturally past the previous hop's end.
            self._committed_ms = max(self._committed_ms, segment.end_ms)
            self._last_emitted_text_norm = norm
            self._last_emitted_words = {
                _normalize_segment_text(w) for w in text.split()
            }
            segments.append(segment)
        return segments

    def flush(self) -> list[Segment]:
        """Emit any final tokens that haven't been committed yet.

        On session end, the last hop may have a token whose ``end_ms``
        is past the committed cursor (because the cursor is held
        back). Calling ``flush`` advances the cursor to the end of
        the most recent segment and returns the final uncommitted
        text.
        """
        if not self._buffer:
            return []
        tokens = self._backend.transcribe(bytes(self._buffer), self._sample_rate)
        new_tokens = []
        for tok in tokens:
            absolute_start = self._buffer_start_ms + tok.start_ms
            absolute_end = self._buffer_start_ms + tok.end_ms
            if absolute_end <= self._committed_ms:
                continue
            if absolute_start < self._committed_ms:
                tok_norm = _normalize_segment_text(tok.text)
                if tok_norm and tok_norm in self._last_emitted_words:
                    continue
            new_tokens.append(Token(
                text=tok.text,
                start_ms=absolute_start,
                end_ms=absolute_end,
                sentence_id=tok.sentence_id,
            ))
        if not new_tokens:
            return []
        segments: list[Segment] = []
        for sentence_id, group in _group_by_sentence_id(new_tokens):
            # Strip leading-space tokenization marker (see feed() for the rationale).
            text = " ".join(t.text.lstrip() for t in group)
            segment = Segment(
                text=text,
                start_ms=group[0].start_ms,
                end_ms=group[-1].end_ms,
                sentence_id=sentence_id,
            )
            segments.append(segment)
        self._committed_ms = max(
            self._committed_ms, segments[-1].end_ms,
        )
        self._last_emitted_words = {
            _normalize_segment_text(w)
            for w in segments[-1].text.split()
        }
        return segments


def streaming_from_tokens(
    backend: "StreamingBackend",
    sample_rate: int = 16000,
    hop_ms: int = 1000,
    window_ms: int = 5000,
    vad_mode: str | None = None,
    vad_aggressiveness: int = 3,
) -> StreamingTranscriber:
    """Build a :class:`StreamingTranscriber` from a token-returning backend.

    Use this when the backend supports word-level timestamps
    (mlx-whisper with ``word_timestamps=True``). ``vad_mode`` opts into the
    pre-Whisper spectral VAD gate (see :class:`StreamingTranscriber`)."""
    return StreamingTranscriber(
        backend, sample_rate, hop_ms, window_ms, is_token_backend=True,
        vad_mode=vad_mode, vad_aggressiveness=vad_aggressiveness,
    )


def streaming_from_text(
    transcriber: Transcriber,
    sample_rate: int = 16000,
    hop_ms: int = 1000,
    window_ms: int = 5000,
) -> StreamingTranscriber:
    """Build a :class:`StreamingTranscriber` from a str-returning legacy
    :class:`Transcriber`. The transcriber's whole output is treated as
    one token per hop (no internal timestamps). Better than hard cuts
    at 5s, but not as good as :func:`streaming_from_tokens`."""
    return StreamingTranscriber(
        transcriber, sample_rate, hop_ms, window_ms, is_token_backend=False,
    )
