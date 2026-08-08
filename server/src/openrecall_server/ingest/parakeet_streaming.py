"""Production streaming Parakeet backend (parakeet-mlx, Apple Silicon / Mac).

An alternative to :class:`~openrecall_server.ingest.whisper_streaming.WhisperStreamingBackend`
that implements the same
:class:`~openrecall_server.ingest.streaming_transcriber.StreamingBackend`
Protocol, so the two are interchangeable behind
:func:`~openrecall_server.ingest.streaming_transcriber.streaming_from_tokens`.

**Why a second backend.** Whisper's decoder is autoregressive: it is forced to
emit *some* token at every step, so near-silent / low-SNR audio produces short,
confident training phrases ("Thank you.", "Thanks for watching.") that pass the
no_speech_prob / avg_logprob gates. The server-side blocklist and cross-hop
dedup patch that after the fact. NVIDIA's Parakeet-TDT uses a Token-and-Duration
Transducer decoder, which emits an explicit *blank* symbol for non-speech
frames — silence maps to no text at all. That removes the hallucination at the
architecture rather than filtering it downstream, which is the reason this
backend exists.

Consequently the Whisper-specific filters (``no_speech_threshold``,
``logprob_threshold``, ``compression_ratio_threshold``, the hallucination
phrase blocklist) have **no analogue here** — they read mlx-whisper segment
metadata that Parakeet does not produce, and the failure mode they defend
against does not occur. The backend-agnostic defenses still apply: the
cross-hop short-phrase dedup and the opt-in webrtcvad gate both live in
:class:`StreamingTranscriber`, above this seam.

Heavy deps (``mlx``, ``numpy``, ``parakeet_mlx``) are imported lazily inside
:meth:`_featurize` / :meth:`_ensure_model` so importing this module — and
running the unit suite — never loads them. Tests inject fakes via the
``model`` and ``featurize`` seams.

Install the extra and run on the Mac:

    pip install -e '.[parakeet]'

Note that unlike the Whisper path this backend never shells out to ffmpeg:
``parakeet_mlx``'s public ``model.transcribe(path)`` loads audio from disk via
an ffmpeg subprocess, which is unusable for a live 1 s hop. We feed the
in-memory PCM straight through the same internal path that method uses
(``get_logmel`` -> ``model.generate``), so no temp files and no subprocess per
hop.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .streaming_transcriber import Token

logger = logging.getLogger(__name__)

# int16 full-scale; PCM bytes -> float32 in [-1, 1), matching what
# parakeet_mlx.audio.load_audio produces from ffmpeg's s16le output.
_INT16_FULL_SCALE = 32768.0

# Parakeet-TDT v3: 0.6B params, 25 European languages, Apache 2.0. The
# transducer decoder is what buys the silence robustness described above.
DEFAULT_PARAKEET_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"

# Parakeet's acoustic frontend is fixed at 16 kHz, which is also the V1 audio
# spec's rate, so a mismatch is a wiring bug rather than something to resample.
_EXPECTED_SAMPLE_RATE = 16000


def _seconds_to_ms(seconds: float | None) -> int:
    """Convert a Parakeet timestamp (float seconds) to integer ms.

    Mirrors the Whisper backend's helper: rounds to the nearest millisecond,
    and clamps negative / non-finite values to 0 so the streaming wrapper's
    committed cursor never sees a nonsense value.
    """
    if seconds is None or seconds < 0 or seconds != seconds:  # NaN-safe
        return 0
    return round(seconds * 1000)


def _aligned_tokens_to_tokens(aligned: list[Any]) -> list[Token]:
    """Regroup Parakeet's sub-word tokens into whole-word :class:`Token`s.

    Parakeet emits *sentencepiece sub-word* pieces whose text carries the word
    boundary as a leading space — ``parakeet_mlx`` reconstructs a sentence with
    ``"".join(t.text for t in tokens)``. The
    :class:`~openrecall_server.ingest.streaming_transcriber.StreamingTranscriber`
    above us instead joins its tokens with ``" "``, because it expects
    word-level tokens. Emitting raw sub-words into that wrapper would produce
    mangled text ("Hel lo wor ld"), so we merge each run of sub-words into one
    word-level Token here:

    - a piece whose text begins with a space starts a new word,
    - the word's ``start_ms`` is its first piece's start, its ``end_ms`` the
      last piece's end,
    - the text is stripped, so the wrapper's ``" "`` join reproduces normal
      spacing.

    Pieces with a zero/negative span are still merged for their *text* (a
    sub-word can legitimately carry a degenerate duration), but a fully
    collapsed word (``end <= start``) is dropped rather than emitted as a
    zero-length token, matching the Whisper backend's contract with the
    dedup cursor.
    """
    words: list[Token] = []
    cur_text: str = ""
    cur_start: int | None = None
    cur_end: int = 0

    def flush() -> None:
        nonlocal cur_text, cur_start, cur_end
        text = cur_text.strip()
        if text and cur_start is not None and cur_end > cur_start:
            words.append(Token(text=text, start_ms=cur_start, end_ms=cur_end))
        cur_text = ""
        cur_start = None
        cur_end = 0

    for piece in aligned:
        raw = getattr(piece, "text", "")
        if not raw:
            continue
        start_ms = _seconds_to_ms(getattr(piece, "start", None))
        end_ms = _seconds_to_ms(getattr(piece, "end", None))
        # A leading space is sentencepiece's word-boundary marker. The very
        # first piece of a window may lack it, so an in-progress word is only
        # flushed when we actually see a boundary.
        if raw[:1].isspace() and cur_text:
            flush()
        if cur_start is None:
            cur_start = start_ms
        cur_text += raw
        cur_end = max(cur_end, end_ms)
    flush()
    return words


def _result_to_tokens(result: Any) -> list[Token]:
    """Extract word-level Tokens from a ``parakeet_mlx`` ``AlignedResult``.

    ``AlignedResult`` exposes a flattened ``tokens`` property over its
    ``sentences``; we prefer that and fall back to walking ``sentences``
    directly so a future version that drops the convenience property still
    works. A result with neither yields no tokens rather than raising — an
    empty hop (silence) is the normal, expected case for this backend.
    """
    if result is None:
        return []
    aligned = getattr(result, "tokens", None)
    if aligned is None:
        aligned = [
            tok
            for sentence in getattr(result, "sentences", []) or []
            for tok in getattr(sentence, "tokens", []) or []
        ]
    return _aligned_tokens_to_tokens(list(aligned))


# The injectable featurizer seam: int16 PCM bytes + sample rate -> whatever
# ``model.generate`` accepts as its mel argument. Production wires the real
# numpy/mlx/get_logmel chain; tests inject a fake.
FeaturizeFn = Callable[[bytes, int], Any]


class ParakeetStreamingBackend:
    """Token-returning streaming backend backed by parakeet-mlx.

    Wire it with
    :func:`~openrecall_server.ingest.streaming_transcriber.streaming_from_tokens`
    exactly like :class:`WhisperStreamingBackend`; the streaming wrapper feeds
    it a rolling window once per hop and dedups by word timestamp.

    ``model`` and ``featurize`` are test seams. In production both are
    ``None``: the model is lazily loaded from ``model_name`` on the first
    :meth:`transcribe` call (so constructing the backend at gateway startup
    costs nothing and never imports mlx), and the featurizer is the real
    ``get_logmel`` chain.
    """

    def __init__(
        self,
        model: Any | None = None,
        model_name: str = DEFAULT_PARAKEET_MODEL,
        featurize: FeaturizeFn | None = None,
    ) -> None:
        self._model = model
        self._model_name = model_name
        self._featurize = featurize

    def _ensure_model(self) -> Any:
        """Load the Parakeet model on first use (lazy, then cached).

        The import and the weight download/load both happen here rather than
        in ``__init__`` so that building the pipeline factory — which happens
        unconditionally at gateway startup, before we know whether audio will
        ever arrive — stays free and dependency-light.
        """
        if self._model is None:
            from parakeet_mlx import from_pretrained

            logger.info("loading Parakeet model %s (first use)", self._model_name)
            self._model = from_pretrained(self._model_name)
            logger.info("Parakeet model %s ready", self._model_name)
        return self._model

    def _default_featurize(self, pcm: bytes, sample_rate: int) -> Any:
        """int16 PCM bytes -> log-mel features for ``model.generate``.

        This is the in-memory equivalent of ``parakeet_mlx``'s
        ``load_audio`` + ``get_logmel``: that path decodes a *file* through an
        ffmpeg subprocess, which we cannot use for a live hop. ffmpeg's output
        is s16le normalized by 32768.0, so converting our PCM the same way
        reproduces its array exactly.
        """
        import mlx.core as mx
        import numpy as np
        from parakeet_mlx.audio import get_logmel

        model = self._ensure_model()
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / _INT16_FULL_SCALE
        # The model's own frontend config drives the mel parameters; reading it
        # from the model (rather than hardcoding) keeps this correct across
        # Parakeet variants (0.6b / 1.1b, TDT / CTC).
        return get_logmel(mx.array(audio), model.preprocessor_config)

    def transcribe(self, pcm: bytes, sample_rate: int) -> list[Token]:
        if sample_rate != _EXPECTED_SAMPLE_RATE:
            # Parakeet's frontend is fixed at 16 kHz and the V1 audio spec is
            # 16 kHz, so refuse rather than silently mis-transcribe. Same
            # contract as the Whisper backend.
            raise ValueError(f"expected 16 kHz PCM, got {sample_rate} Hz")
        if not pcm:
            return []

        featurize = self._featurize or self._default_featurize
        mel = featurize(pcm, sample_rate)
        model = self._ensure_model()

        results = model.generate(mel)
        # ``generate`` returns a list of AlignedResult (one per batch item); we
        # always pass a single window, so take the first. An empty list means
        # the transducer emitted only blanks — i.e. silence, the case this
        # backend is here to handle gracefully.
        result = results[0] if results else None
        tokens = _result_to_tokens(result)
        logger.debug(
            "parakeet streaming: %d PCM bytes -> %d word tokens", len(pcm), len(tokens),
        )
        return tokens
