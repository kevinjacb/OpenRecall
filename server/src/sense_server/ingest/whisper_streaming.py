"""Production streaming Whisper backend (Mlx-whisper, Apple Silicon / Mac).

The :class:`WhisperStreamingBackend` wraps ``mlx_whisper.transcribe`` with
``word_timestamps=True`` and converts the output into a list of
:class:`~sense_server.ingest.streaming_transcriber.Token` shaped for
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


def _mlx_segments_to_tokens(response: dict) -> list[Token]:
    """Translate mlx-whisper's output dict into a flat list of Tokens.

    A segment with no ``words`` array (silence, or a hallucinated empty
    line) is skipped — we have nothing to commit. Words with
    ``start >= end`` are malformed and are dropped, since they would
    confuse the streaming wrapper's dedup cursor.

    Note: mlx-whisper prefixes non-first word-tokens with a single
    space (a tokenization marker, NOT content). The streaming wrapper
    joins tokens with ``""`` (empty string) so the leading space IS
    the word boundary — it reconstructs ``"Hello, world!"`` from
    ``["Hello,", " world!"]``. We preserve the leading space here.
    """
    tokens: list[Token] = []
    for seg in response.get("segments", []):
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
    return tokens


# Type alias for the injected mlx-whisper callable. Real signature is
# ``mlx_whisper.transcribe(audio_array, path_or_hf_repo=..., **kwargs)``
# but we only need a callable for the seam; production wires the real
# function at startup.
MlxTranscribeFn = Callable[..., dict]


class WhisperStreamingBackend:
    """Token-returning streaming backend backed by mlx-whisper.

    Use this with :func:`~sense_server.ingest.streaming_transcriber.streaming_from_tokens`
    to wire it into a :class:`StreamingTranscriber`. The streaming
    transcriber then feeds rolling 5s windows of audio to this backend
    once per hop and uses the returned word_timestamps to dedup the
    overlap zone — eliminating the boundary-loss artifacts of the
    hard-cut pipeline.
    """

    def __init__(
        self,
        mlx_transcribe: MlxTranscribeFn | None = None,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self._mlx_transcribe = mlx_transcribe
        self._model = model

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
            self._model,  # positional: matches mlx_whisper.transcribe's
                           # `(audio, path_or_hf_repo, ...)` signature.
            word_timestamps=True,
        )
        tokens = _mlx_segments_to_tokens(response)
        if tokens:
            print(
                f"whisper streaming: {len(audio)} samples -> {len(tokens)} tokens"
            )
        return tokens
