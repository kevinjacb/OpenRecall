"""Production streaming Whisper backend (Mlx-whisper, Apple Silicon / Mac).

The :class:`WhisperStreamingBackend` wraps ``mlx_whisper.transcribe`` with
``word_timestamps=True`` and converts the output into a list of
:class:`~opensapien_server.ingest.streaming_transcriber.Token` shaped for
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

from collections.abc import Callable
from typing import Any

from .streaming_transcriber import Token

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


def _mlx_segments_to_tokens(
    response: dict,
    no_speech_threshold: float = 0.6,
    logprob_threshold: float = -1.0,
) -> list[Token]:
    """Translate mlx-whisper's output dict into a flat list of Tokens.

    A segment with no ``words`` array (silence, or a hallucinated empty
    line) is skipped — we have nothing to commit. Words with
    ``start >= end`` are malformed and are dropped, since they would
    confuse the streaming wrapper's dedup cursor.

    **Noise filtering** (server-side defense against quiet-input
    hallucination): a segment is dropped if either
    ``no_speech_prob > no_speech_threshold`` (mlx is confident the
    audio is silence) OR
    ``avg_logprob < logprob_threshold`` (mlx is uncertain about
    what it heard). A missing field is treated as low-risk
    (conservatively kept) so older mlx-whisper versions still work.

    Note: mlx-whisper prefixes non-first word-tokens with a single
    space (a tokenization marker, NOT content). The streaming wrapper
    joins tokens with ``""`` (empty string) so the leading space IS
    the word boundary — it reconstructs ``"Hello, world!"`` from
    ``["Hello,", " world!"]``. We preserve the leading space here.
    """
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
        # Repeated-token hallucination: a real word looped on near-silence
        # has good confidence signals, so the checks above let it through.
        # Drop the whole segment if its words are unambiguously repetitive.
        if _is_hallucinated_repetition(seg.get("words") or []):
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

    Use this with :func:`~opensapien_server.ingest.streaming_transcriber.streaming_from_tokens`
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
    ) -> None:
        self._mlx_transcribe = mlx_transcribe
        self._model = model
        self._no_speech_threshold = no_speech_threshold
        self._logprob_threshold = logprob_threshold

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
        )
        tokens = _mlx_segments_to_tokens(
            response,
            no_speech_threshold=self._no_speech_threshold,
            logprob_threshold=self._logprob_threshold,
        )
        if tokens:
            print(
                f"whisper streaming: {len(audio)} samples -> {len(tokens)} tokens"
            )
        return tokens
