"""Production streaming Whisper backend (Mlx-whisper, Apple Silicon / Mac).

The :class:`WhisperStreamingBackend` wraps ``mlx_whisper.transcribe`` with
``word_timestamps=True`` and converts the output into a list of
:class:`~openrecall_server.ingest.streaming_transcriber.Token` shaped for
the streaming transcriber.

Heavy deps (``mlx``, ``numpy``) are imported lazily inside
:meth:`transcribe` so unit tests never load them. The default model
is ``whisper-large-v3-turbo`` — a strong accuracy/speed point on
Apple Silicon with 48 GB. Swap to a distilled/medium model if you
need a faster RTF.

The class accepts an injected ``mlx_transcribe`` callable for tests;
production wires the real ``mlx_whisper.transcribe`` at gateway
startup (or per-call, depending on the deployment).

Install the extra and run on the Mac:

    pip install -e '.[mlx]'
"""
from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

from .streaming_transcriber import Token

logger = logging.getLogger(__name__)

# int16 full-scale; PCM bytes -> float32 in [-1, 1) for Whisper.
_INT16_FULL_SCALE = 32768.0

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"


def _seconds_to_ms(seconds: float) -> int:
    """Convert a Whisper timestamp (float seconds) to integer ms.

    Rounds to the nearest millisecond. Negative or non-finite values
    are clamped to 0 so downstream dedup logic doesn't see weird
    cursors.
    """
    if seconds is None or seconds < 0 or seconds != seconds:  # NaN-safe
        return 0
    return round(seconds * 1000)


# --- repeated-token hallucination detector ----------------------------------
# mlx-whisper's other classic failure mode on near-silence / low-SNR audio:
# it transcribes a real word and then loops it many times in one segment.
# These segments pass the no_speech_prob / avg_logprob filter (they ARE real
# words, just wrongly repeated), so without this detector they reach the
# streaming wrapper and get stored as gibberish like
# "Congratulations. Congratulations. Congratulations. Congratulations."
# (6x in 640 ms — physically impossible as speech). This matters most once
# the firmware VAD is sensitive enough to admit near-silence frames, which
# is exactly when Whisper hallucinates. Two signals, both required to be
# unambiguous before we drop real speech:
#   - a run of >= HALLUC_MIN_RUN consecutive identical tokens, or
#   - >= HALLUC_MIN_TOKENS tokens whose unique/total ratio is below
#     HALLUC_MAX_UNIQUE_RATIO (a tiny vocabulary packed into a long segment).
_HALLUC_MIN_RUN = 5
_HALLUC_MIN_TOKENS = 6
_HALLUC_MAX_UNIQUE_RATIO = 0.5


def _normalize_word(text: str) -> str:
    """Lowercase + strip whitespace/punctuation so that
    "Congratulations." == "Congratulations" == "congratulations"."""
    return text.strip().lower().strip(".,!?;:'\"-()[]{}")


def _texts_are_hallucinated_repetition(texts: list[str]) -> bool:
    """True if a sequence of word strings looks like Whisper's repeated-token
    hallucination (a real word looped on near-silence).

    Conservative by design: short sequences and ordinary emphatic repeats
    ("no no no", "yeah yeah") are kept. Only unambiguous looping is dropped.
    """
    if not texts:
        return False
    norm = [_normalize_word(t) for t in texts]

    # Rule 1: a long run of the same token.
    run = 1
    max_run = 1
    for i in range(1, len(norm)):
        if norm[i] and norm[i] == norm[i - 1]:
            run += 1
            if run > max_run:
                max_run = run
        else:
            run = 1
    if max_run >= _HALLUC_MIN_RUN:
        return True

    # Rule 2: very low unique-token ratio across enough tokens.
    if len(norm) >= _HALLUC_MIN_TOKENS:
        unique = len({n for n in norm if n})
        if unique / len(norm) < _HALLUC_MAX_UNIQUE_RATIO:
            return True
    return False


def _is_hallucinated_repetition(words: list[dict]) -> bool:
    """Per-segment check: extract word strings from mlx-whisper's word dicts
    and apply the repetition test."""
    return _texts_are_hallucinated_repetition(
        [w.get("word", "") for w in words if w.get("word")]
    )


# --- short-phrase hallucination blocklist ------------------------------------
# The repetition detector above catches a *long* looping segment. Whisper's
# other near-silence failure mode is the opposite: a *short, single, confident*
# training phrase — "Thank you.", "Hello.", "Thanks for watching." — emitted
# on room noise the firmware VAD admitted. These pass no_speech_prob /
# avg_logprob (they ARE confident predictions), so the confidence gate lets
# them through and they get stored as phantom transcripts. The blocklist drops
# a SHORT segment whose normalized text matches a known phantom. The word-
# count gate keeps a real sentence that merely contains "thank you".
#
# Built from Whisper's documented silent-audio hallucinations + the phrases
# this device's user observed in production. Tunable via
# ``OPENRECALL_WHISPER_HALLUCINATION_PHRASES`` (comma-separated; overrides).
#
# Deliberately EXCLUDES bare common greetings ("hello", "hi"): a text
# blocklist cannot tell a real isolated greeting from a phantom one, and
# dropping a real "Hello." is a worse failure mode than the user fixing
# the phantom case with one env line. "thank you" is included because the
# user reported it and an isolated "thank you." on a memory wearable is
# far more often a sign-off phantom than real speech. To block "hello" on
# a device that hallucinates it, set OPENRECALL_WHISPER_HALLUCINATION_PHRASES
# to the full desired list, or enable the spectral VAD gate (vad_mode).
_DEFAULT_HALLUCINATION_PHRASES = (
    "thank you",
    "thanks for watching",
    "thank you for watching",
    "thanks for listening",
    "thank you for listening",
    "please subscribe",
    "please consider subscribing",
    "subscribe",
    "by amara",
    "amara",
)


def _normalize_phrase(text: str) -> str:
    """Normalize a transcript phrase for blocklist matching: lowercase, strip
    leading/trailing punctuation, collapse internal whitespace. Idempotent
    so pre-normalized inputs (already lowercase) are unchanged."""
    stripped = text.strip().lower()
    # Strip the punctuation Whisper loves to append: . , ! ? ; : and quotes.
    stripped = stripped.strip(".,!?;:'\"-()[]{}“”")
    # Collapse runs of whitespace (mlx word-tokens join with single spaces, but
    # be robust to any internal spacing).
    return " ".join(stripped.split())


def _resolve_blocklist(
    enabled: bool,
    phrases: tuple[str, ...] | None,
) -> frozenset[str] | None:
    """Build the normalized blocklist frozenset, or None when disabled.

    ``None`` means "do not apply the blocklist" (the per-segment check is
    skipped). An explicit empty tuple means "block nothing" (a deliberate
    operator override) and yields an empty frozenset, which the caller's
    ``in`` test handles correctly (matches nothing).
    """
    if not enabled:
        return None
    src = phrases if phrases is not None else _DEFAULT_HALLUCINATION_PHRASES
    return frozenset(_normalize_phrase(p) for p in src)


def _mlx_segments_to_tokens(
    response: dict,
    no_speech_threshold: float = 0.6,
    logprob_threshold: float = -1.0,
    compression_ratio_threshold: float = 2.4,
    hallucination_blocklist_enabled: bool = True,
    hallucination_max_words: int = 4,
    hallucination_phrases: tuple[str, ...] | None = None,
) -> list[Token]:
    """Translate mlx-whisper's output dict into a flat list of Tokens.

    A segment with no ``words`` array (silence, or a hallucinated empty
    line) is skipped — we have nothing to commit. Words with
    ``start >= end`` are malformed and are dropped, since they would
    confuse the streaming wrapper's dedup cursor.

    **Noise filtering** (server-side defense against quiet-input
    hallucination): a segment is dropped if any of these hold:

    - ``no_speech_prob > no_speech_threshold`` (mlx is confident the
      audio is silence),
    - ``avg_logprob < logprob_threshold`` (mlx is uncertain about what
      it heard),
    - ``compression_ratio > compression_ratio_threshold`` (the segment
      is too repetitive — a gzip-ratio hallucination signature), or
    - it is a *short* segment (``<= hallucination_max_words`` words)
      whose normalized text matches a known hallucination phrase in
      the blocklist.

    A missing field is treated as low-risk (conservatively kept) so
    older mlx-whisper versions still work.

    Note: mlx-whisper prefixes non-first word-tokens with a single
    space (a tokenization marker, NOT content). The streaming wrapper
    joins tokens with ``""`` (empty string) so the leading space IS
    the word boundary — it reconstructs ``"Hello, world!"`` from
    ``["Hello,", " world!"]``. We preserve the leading space here.
    """
    blocklist = _resolve_blocklist(
        hallucination_blocklist_enabled, hallucination_phrases,
    )
    tokens: list[Token] = []
    for seg in response.get("segments", []):
        # Per-segment confidence gating. Missing fields are treated as
        # low-risk so a future mlx-whisper version without these
        # fields (or a custom backend) doesn't silently drop everything.
        no_speech_prob = seg.get("no_speech_prob")
        if no_speech_prob is not None and no_speech_prob > no_speech_threshold:
            continue
        avg_logprob = seg.get("avg_logprob")
        if avg_logprob is not None and avg_logprob < logprob_threshold:
            continue
        # compression_ratio: mlx already applies this as a temperature
        # fallback, but a segment can still surface with a high ratio
        # (e.g. after the fallback gives up). Drop it as a backstop.
        compression_ratio = seg.get("compression_ratio")
        if compression_ratio is not None and compression_ratio > compression_ratio_threshold:
            continue
        # Repeated-token hallucination: a real word looped on near-silence
        # has good confidence signals, so the checks above let it through.
        # Drop the whole segment if its words are unambiguously repetitive.
        if _is_hallucinated_repetition(seg.get("words") or []):
            continue
        # Short-phrase blocklist: a short, confident phantom ("Thank you.",
        # "Hello.") on noise. Gated on word count so a real sentence that
        # contains the phrase is kept.
        if blocklist is not None:
            seg_words = seg.get("words") or []
            word_count = len(seg_words) or len(seg.get("text", "").split())
            if (
                word_count <= hallucination_max_words
                and _normalize_phrase(seg.get("text", "")) in blocklist
            ):
                continue
        for word in seg.get("words") or []:
            text = word.get("word", "")
            if not text:
                continue
            start_s = word.get("start")
            end_s = word.get("end")
            if start_s is None or end_s is None:
                continue
            start_ms = _seconds_to_ms(start_s)
            end_ms = _seconds_to_ms(end_s)
            if end_ms <= start_ms:
                # Malformed word; drop rather than emit a zero-length token.
                continue
            tokens.append(Token(text=text, start_ms=start_ms, end_ms=end_ms))
    # Aggregate guard: Whisper can also split a looping hallucination across
    # many short one-word segments, each individually non-repetitive. If the
    # whole response's tokens are unambiguatively repetitive, drop them all.
    # Normal multi-segment speech has a high unique-token ratio, so this only
    # fires when the entire call hallucinated.
    if tokens and _texts_are_hallucinated_repetition([t.text for t in tokens]):
        return []
    return tokens


# Type alias for the injected mlx-whisper callable. Real signature is
# ``mlx_whisper.transcribe(audio_array, path_or_hf_repo=..., **kwargs)``
# but we only need a callable for the seam; production wires the real
# function at startup.
MlxTranscribeFn = Callable[..., dict]


class WhisperStreamingBackend:
    """Token-returning streaming backend backed by mlx-whisper.

    Use this with :func:`~openrecall_server.ingest.streaming_transcriber.streaming_from_tokens`
    to wire it into a :class:`StreamingTranscriber`. The streaming
    transcriber then feeds rolling 5s windows of audio to this backend
    once per hop and uses the returned word_timestamps to dedup the
    overlap zone — eliminating the boundary-loss artifacts of the
    hard-cut pipeline.

    ``no_speech_threshold`` and ``logprob_threshold`` are *server-side*
    defenses against quiet-input hallucination. The firmware's VAD
    also gates which frames get sent; this is the second line of
    defense, and it's particularly important when the XIAO onboard
    mic's VAD fires on room noise. Default values match mlx-whisper's
    defaults.
    """

    def __init__(
        self,
        mlx_transcribe: MlxTranscribeFn | None = None,
        model: str = DEFAULT_MODEL,
        no_speech_threshold: float = 0.6,
        logprob_threshold: float = -1.0,
        compression_ratio_threshold: float = 2.4,
        condition_on_previous_text: bool = False,
        hallucination_blocklist_enabled: bool = True,
        hallucination_max_words: int = 4,
        hallucination_phrases: tuple[str, ...] | None = None,
    ) -> None:
        self._mlx_transcribe = mlx_transcribe
        self._model = model
        self._no_speech_threshold = no_speech_threshold
        self._logprob_threshold = logprob_threshold
        self._compression_ratio_threshold = compression_ratio_threshold
        # condition_on_previous_text=False (the streaming default) stops a
        # hop's hallucinated output from being fed back as the next hop's
        # prompt — mlx-whisper's own default of True propagates phantoms
        # across the rolling window. Each hop carries its own 5s context, so
        # disabling it does not lose real cross-window continuity here.
        self._condition_on_previous_text = condition_on_previous_text
        self._hallucination_blocklist_enabled = hallucination_blocklist_enabled
        self._hallucination_max_words = hallucination_max_words
        self._hallucination_phrases = hallucination_phrases

    def transcribe(self, pcm: bytes, sample_rate: int) -> list[Token]:
        if sample_rate != 16000:
            # Whisper operates at 16 kHz; the V1 audio spec is 16 kHz,
            # so refuse rather than silently mis-transcribe a wrong-rate
            # buffer.
            raise ValueError(f"expected 16 kHz PCM, got {sample_rate} Hz")

        import numpy as np

        # int16 LE bytes -> float32 in [-1, 1)
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / _INT16_FULL_SCALE

        # Lazy import of mlx_whisper — unit tests inject a fake.
        if self._mlx_transcribe is None:
            import mlx_whisper
            transcribe = mlx_whisper.transcribe
        else:
            transcribe = self._mlx_transcribe

        response = transcribe(
            audio,
            path_or_hf_repo=self._model,  # keyword-only in mlx_whisper.transcribe
            word_timestamps=True,
            no_speech_threshold=self._no_speech_threshold,
            logprob_threshold=self._logprob_threshold,
            compression_ratio_threshold=self._compression_ratio_threshold,
            condition_on_previous_text=self._condition_on_previous_text,
        )
        tokens = _mlx_segments_to_tokens(
            response,
            no_speech_threshold=self._no_speech_threshold,
            logprob_threshold=self._logprob_threshold,
            compression_ratio_threshold=self._compression_ratio_threshold,
            hallucination_blocklist_enabled=self._hallucination_blocklist_enabled,
            hallucination_max_words=self._hallucination_max_words,
            hallucination_phrases=self._hallucination_phrases,
        )
        # Instrumentation: a debug log so an operator can confirm the false-
        # positive hypothesis and calibrate the blocklist / thresholds from
        # real hops. Enable with DEBUG logging on this module. RMS dBFS is
        # the input's loudness (0 = full scale; the firmware VAD admits frames
        # down to ~-43 dBFS), no_speech_prob/avg_logprob are mlx's confidence,
        # and the text is what survived the filters.
        if logger.isEnabledFor(logging.DEBUG) and response.get("segments"):
            try:
                rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
                rms_dbfs = (20.0 * math.log10(rms)) if rms > 0 else -math.inf
            except Exception:
                rms_dbfs = 0.0
            for seg in response.get("segments", []):
                logger.debug(
                    "whisper segment: rms_dbfs=%.1f no_speech_prob=%s "
                    "avg_logprob=%s compression_ratio=%s text=%r",
                    rms_dbfs,
                    seg.get("no_speech_prob"),
                    seg.get("avg_logprob"),
                    seg.get("compression_ratio"),
                    seg.get("text", ""),
                )
        logger.debug(
            "whisper streaming: %d samples -> %d tokens", len(audio), len(tokens),
        )
        return tokens
