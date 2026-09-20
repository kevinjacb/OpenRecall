"""Portable streaming Whisper backend (faster-whisper / CTranslate2, CPU + CUDA).

The third implementation of the
:class:`~openrecall_server.ingest.streaming_transcriber.StreamingBackend`
Protocol, alongside :class:`WhisperStreamingBackend` (mlx-whisper) and
:class:`ParakeetStreamingBackend` (parakeet-mlx). Both of those run on MLX and
are therefore **Apple-Silicon-only**, which blocks every non-Apple deployment
target. faster-whisper runs the *same* code on CPU and on CUDA — the only
difference is the ``device`` argument handed to CTranslate2 — so one backend
serves both the ``cpu`` and ``nvidia`` deployment profiles, and it can be
developed and verified on a Mac's CPU before any GPU exists.

**It is still Whisper.** The model weights and the decoder are OpenAI Whisper's
(CTranslate2 is a fast inference engine, not a different model), so every
failure mode :mod:`whisper_streaming` defends against applies here verbatim:
short confident phantom phrases on near-silence ("Thank you.", "Thanks for
watching."), looping repeated tokens, and low-confidence noise segments. That
is why this module *imports* those defenses from :mod:`whisper_streaming`
rather than re-deriving them: there must be exactly one copy of the blocklist
and the repetition detector, or the two Whisper backends drift apart.

Knob equivalence with :class:`WhisperStreamingBackend`:

===============================  ==========================================
WhisperStreamingBackend          here
===============================  ==========================================
``model`` (mlx repo id)          ``model`` (a size like ``large-v3-turbo``,
                                 or a CTranslate2 model dir / HF repo id)
``no_speech_threshold``          same (passed through + post-filter)
``logprob_threshold``            same — but faster-whisper spells the
                                 transcribe kwarg ``log_prob_threshold``
``compression_ratio_threshold``  same
``condition_on_previous_text``   same
``hallucination_*``              same (shared helpers)
``inference_lock``               same
``mlx_transcribe`` (test seam)   ``loaded_model``: an already-constructed
                                 ``WhisperModel`` (or a fake). mlx-whisper's
                                 seam is a module-level *function*;
                                 faster-whisper's unit of work is a model
                                 *object*, so the seam is the object.
===============================  ==========================================

No equivalent, deliberately dropped:

* ``vad_mode`` / ``vad_aggressiveness`` — those are not backend knobs at all;
  they live in :class:`StreamingTranscriber`, above this seam, and apply to
  whichever backend is selected. faster-whisper *also* ships its own Silero
  ``vad_filter``; it is left OFF so there is one VAD policy in the system
  rather than two disagreeing ones.

Heavy deps (``faster_whisper``, ``numpy``) are imported lazily inside
:meth:`transcribe` / :meth:`_ensure_model`, never at module import, so the
gateway, the unit suite and the core container can import this module with
neither installed.

Install the extra:

    pip install -e '.[fasterwhisper]'

and select it at runtime with ``OPENRECALL_ASR_BACKEND=faster_whisper``.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from .streaming_transcriber import Token

# The Whisper-family noise defenses. Shared, not copied: faster-whisper runs
# the same Whisper decoder as mlx-whisper, so the phantom phrases, the
# repetition loops and the confidence signals are identical, and a second copy
# of the blocklist would silently drift from the tuned one.
from .whisper_streaming import (
    _resolve_blocklist,
    _normalize_phrase,
    _seconds_to_ms,
    _texts_are_hallucinated_repetition,
)

logger = logging.getLogger(__name__)

# int16 full-scale; PCM bytes -> float32 in [-1, 1), which is what
# faster_whisper.decode_audio() produces and what WhisperModel.transcribe()
# expects when handed a numpy array instead of a path.
_INT16_FULL_SCALE = 32768.0

# Whisper's frontend is fixed at 16 kHz, which is also the V1 audio spec's
# rate, so a mismatch is a wiring bug rather than something to resample.
_EXPECTED_SAMPLE_RATE = 16000

# Same model family as the MLX default (``mlx-community/whisper-large-v3-turbo``)
# so switching backends does not silently change transcription quality.
# faster-whisper resolves this alias to the CTranslate2 conversion on the HF Hub.
# A smaller model ("small.en", "distil-large-v3.5") is one env var away and is
# the right call on a modest CPU box.
DEFAULT_FASTER_WHISPER_MODEL = "large-v3-turbo"

# "auto" == CUDA when a GPU is visible, else CPU. Explicit "cpu"/"cuda" pins it.
DEFAULT_DEVICE = "auto"

# "default" keeps the model's own saved precision, with CTranslate2 falling back
# to something the device supports (float32 on a CPU that has no float16 path).
# It is the accuracy-preserving choice; operators set "int8" on CPU for ~2-4x
# the speed at a small quality cost, and "float16" on CUDA.
DEFAULT_COMPUTE_TYPE = "default"


def _words_of(segment: Any) -> list[Any]:
    """The segment's word list, tolerating ``None`` (word_timestamps off).

    faster-whisper's ``Segment.words`` is ``None`` — not ``[]`` — when
    ``word_timestamps=False``. We always request them, but a future version or
    a degenerate segment must not crash the hop.
    """
    return list(getattr(segment, "words", None) or [])


def _segments_to_tokens(
    segments: Any,
    no_speech_threshold: float = 0.6,
    logprob_threshold: float = -1.0,
    compression_ratio_threshold: float = 2.4,
    hallucination_blocklist_enabled: bool = True,
    hallucination_max_words: int = 4,
    hallucination_phrases: tuple[str, ...] | None = None,
) -> list[Token]:
    """Translate faster-whisper's ``Segment`` sequence into a flat list of Tokens.

    Structurally identical to
    :func:`~openrecall_server.ingest.whisper_streaming._mlx_segments_to_tokens`
    — same filters, same drop rules, same ``sentence_id`` convention — with one
    difference: faster-whisper yields *objects* with attributes, where
    mlx-whisper yields dicts. Hence ``getattr`` rather than ``.get``.

    ``segments`` is normally a lazy generator: CTranslate2 does no work until it
    is iterated. The caller must therefore consume it where it wants the compute
    to happen (see :meth:`FasterWhisperStreamingBackend.transcribe`, which does
    so under the inference lock).

    Filters, all mirroring the mlx backend:

    - ``no_speech_prob > no_speech_threshold`` (the model is confident this is
      silence),
    - ``avg_logprob < logprob_threshold`` (the model is unsure what it heard),
    - ``compression_ratio > compression_ratio_threshold`` (repetitive
      gibberish),
    - unambiguous repeated-token looping,
    - a *short* segment whose normalized text is a known phantom phrase.

    A missing field is treated as low-risk (kept), so a future faster-whisper
    that drops a field does not silently mute the transcript. Words with
    ``end <= start`` are dropped — a zero-length token would corrupt the
    streaming wrapper's committed cursor.

    ``sentence_id`` is ``index + 1`` *within this call*: the streamer emits one
    Segment per backend segment, and the rolling window renumbers every hop, so
    it must never be compared across calls. ``0`` is the "no structure"
    sentinel.
    """
    blocklist = _resolve_blocklist(
        hallucination_blocklist_enabled, hallucination_phrases,
    )
    tokens: list[Token] = []
    for seg_idx, seg in enumerate(segments):
        no_speech_prob = getattr(seg, "no_speech_prob", None)
        if no_speech_prob is not None and no_speech_prob > no_speech_threshold:
            continue
        avg_logprob = getattr(seg, "avg_logprob", None)
        if avg_logprob is not None and avg_logprob < logprob_threshold:
            continue
        compression_ratio = getattr(seg, "compression_ratio", None)
        if (
            compression_ratio is not None
            and compression_ratio > compression_ratio_threshold
        ):
            continue
        seg_words = _words_of(seg)
        # Repeated-token hallucination: a real word looped on near-silence has
        # good confidence signals, so the gates above let it through.
        if _texts_are_hallucinated_repetition(
            [getattr(w, "word", "") for w in seg_words if getattr(w, "word", "")]
        ):
            continue
        # Short-phrase blocklist: a short, confident phantom on noise. Gated on
        # word count so a real sentence containing the phrase survives.
        if blocklist is not None:
            seg_text = getattr(seg, "text", "") or ""
            word_count = len(seg_words) or len(seg_text.split())
            if (
                word_count <= hallucination_max_words
                and _normalize_phrase(seg_text) in blocklist
            ):
                continue
        for word in seg_words:
            text = getattr(word, "word", "")
            if not text:
                continue
            start_s = getattr(word, "start", None)
            end_s = getattr(word, "end", None)
            if start_s is None or end_s is None:
                continue
            start_ms = _seconds_to_ms(start_s)
            end_ms = _seconds_to_ms(end_s)
            if end_ms <= start_ms:
                # Malformed word; drop rather than emit a zero-length token.
                continue
            tokens.append(Token(
                text=text, start_ms=start_ms, end_ms=end_ms,
                sentence_id=seg_idx + 1,  # 0 is the "no structure" sentinel.
            ))
    # Aggregate guard: Whisper can split a looping hallucination across many
    # short one-word segments, each individually non-repetitive.
    if tokens and _texts_are_hallucinated_repetition([t.text for t in tokens]):
        return []
    return tokens


def _nvidia_lib_dirs() -> list[str]:
    """Directories of pip-installed NVIDIA runtime libraries, if any.

    The ``nvidia-*-cu12`` wheels install into a PEP 420 **namespace** package,
    so ``nvidia.__file__`` and ``nvidia.cublas.lib.__file__`` are all ``None``
    — the ``os.path.dirname(...__file__)`` recipe in faster-whisper's own docs
    predates that layout and raises TypeError. ``__path__`` is the portable
    way in, and walking it also picks up libraries we did not think to name.
    """
    import os

    try:
        import nvidia
    except ImportError:
        return []
    roots = list(getattr(nvidia, "__path__", []) or [])
    found = {
        dirpath
        for root in roots
        for dirpath, _dirs, files in os.walk(root)
        if any(".so" in name for name in files)
    }
    return sorted(found)


def _preload_cuda_libraries() -> list[str]:
    """dlopen the pip-installed CUDA runtime so CTranslate2 can find it.

    CTranslate2 resolves ``libcublas.so.12`` through the dynamic loader, which
    only consults ``LD_LIBRARY_PATH`` as it was at **process start**. That makes
    the usual advice fragile: export it in the wrong shell, or restart the
    service without it, and the failure returns.

    Loading the libraries here with ``RTLD_GLOBAL`` sidesteps that. dlopen keys
    on SONAME, so once ``libcublas.so.12`` is resident a later
    ``dlopen("libcublas.so.12")`` from CTranslate2 resolves to it regardless of
    the search path. This is the same trick PyTorch uses for its bundled CUDA.

    Best effort by design: returns what it loaded and never raises. If nothing
    is installed there is nothing to do, and a genuinely broken setup still
    fails at the model load with :func:`_cuda_library_hint` explaining it.

    Two passes, because these libraries depend on each other (libcublas needs
    libcublasLt) and the directory order will not always match dependency
    order; a first-pass failure usually succeeds once its dependency is in.
    """
    import ctypes
    import os

    candidates = [
        os.path.join(d, f)
        for d in _nvidia_lib_dirs()
        for f in sorted(os.listdir(d))
        if ".so" in f
    ]
    loaded: list[str] = []
    pending = candidates
    for _attempt in range(2):
        retry: list[str] = []
        for path in pending:
            try:
                ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
                loaded.append(path)
            except OSError:
                retry.append(path)
        if not retry:
            break
        pending = retry
    if loaded:
        logger.info(
            "preloaded %d NVIDIA runtime libraries from the installed wheels "
            "(no LD_LIBRARY_PATH needed)", len(loaded),
        )
    return loaded


def _cuda_library_hint(exc: Exception, device: str) -> Exception:
    """Turn CTranslate2's bare loader error into the actual cause.

    CTranslate2 wheels are built against ONE CUDA major version — the 4.x line
    wants ``libcublas.so.12`` and cuDNN 9. A host with a CUDA 13 toolkit
    provides ``libcublas.so.13``, so the load fails with a message that names a
    missing file and not the reason, which sends people reinstalling drivers or
    downgrading a working toolkit.

    Neither is necessary: the CUDA 12 runtime libraries install from pip and sit
    happily beside a newer toolkit, because the driver is backward compatible.
    They only have to be on the loader path.

    Returns the exception to raise. Anything not obviously a CUDA library
    problem is passed through untouched — a wrong guess here would bury the
    real error under a confident, irrelevant suggestion.
    """
    text = str(exc)
    looks_like_cuda_libs = "libcu" in text or "cudnn" in text.lower()
    if device == "cpu" or not looks_like_cuda_libs:
        return exc
    return RuntimeError(
        f"{text}\n\n"
        "CTranslate2 is built against CUDA 12 (libcublas.so.12, cuDNN 9). A "
        "CUDA 13 toolkit ships libcublas.so.13, so this is a major-version "
        "mismatch, not a broken driver or a bad install — do NOT downgrade the "
        "toolkit. Install the CUDA 12 runtime libraries beside it and put them "
        "on the loader path:\n\n"
        "    pip install nvidia-cublas-cu12 nvidia-cudnn-cu12\n\n"
        "Reinstalling is normally enough: this backend loads those libraries "
        "itself at startup, so no LD_LIBRARY_PATH is required. If it still "
        "fails, set the path explicitly and restart:\n\n"
        "    export LD_LIBRARY_PATH=$(python -c \"import os, nvidia; "
        "print(':'.join(sorted({r for b in nvidia.__path__ for r, _, fs in "
        "os.walk(b) if any('.so' in f for f in fs)})))\")\n\n"
        "(Note: the nvidia wheels are PEP 420 namespace packages, so the "
        "os.path.dirname(...__file__) recipe in faster-whisper's docs raises "
        "TypeError — __file__ is None. Use __path__, as above.)\n\n"
        "See deploy/README.md."
    )


class FasterWhisperStreamingBackend:
    """Token-returning streaming backend backed by faster-whisper (CTranslate2).

    Wire it with
    :func:`~openrecall_server.ingest.streaming_transcriber.streaming_from_tokens`
    exactly like :class:`WhisperStreamingBackend`; the streaming wrapper feeds
    it a rolling window once per hop and dedups by word timestamp.

    ``model`` is the model *identifier* — a Whisper size alias
    (``"large-v3-turbo"``, ``"small.en"``, …), a local CTranslate2 model
    directory, or a CT2 repo id on the HF Hub. It is exactly the role
    ``WhisperStreamingBackend.model`` plays for mlx-whisper.

    ``loaded_model`` is the injection seam: an already-constructed
    ``faster_whisper.WhisperModel`` (production, from the process-wide shared
    cache) or a fake (tests). When it is ``None`` the model is lazily
    constructed on the first :meth:`transcribe` call, so building the backend
    at gateway startup costs nothing and imports nothing.

    ``device`` is ``"cpu"``, ``"cuda"`` or ``"auto"``; ``compute_type`` is a
    CTranslate2 quantization name (``"int8"`` is the usual CPU choice,
    ``"float16"`` the usual CUDA one). These two arguments are the entire
    portability story — nothing else in this class changes between profiles.

    ``language`` pins the decode language (e.g. ``"en"``). Left ``None`` (auto)
    to match the mlx backends' behaviour, but note that auto-detection reruns
    on every hop and can flip mid-session; pin it if the wearer's language is
    known.
    """

    def __init__(
        self,
        model: str = DEFAULT_FASTER_WHISPER_MODEL,
        device: str = DEFAULT_DEVICE,
        compute_type: str = DEFAULT_COMPUTE_TYPE,
        cpu_threads: int = 0,
        loaded_model: Any | None = None,
        no_speech_threshold: float = 0.6,
        logprob_threshold: float = -1.0,
        compression_ratio_threshold: float = 2.4,
        condition_on_previous_text: bool = False,
        beam_size: int = 5,
        language: str | None = None,
        task: str = "transcribe",
        hallucination_blocklist_enabled: bool = True,
        hallucination_max_words: int = 4,
        hallucination_phrases: tuple[str, ...] | None = None,
        inference_lock: threading.Lock | None = None,
    ) -> None:
        self._model_name = model
        self._device = device
        self._compute_type = compute_type
        self._cpu_threads = cpu_threads
        self._model = loaded_model
        self._no_speech_threshold = no_speech_threshold
        self._logprob_threshold = logprob_threshold
        self._compression_ratio_threshold = compression_ratio_threshold
        # False (unlike faster-whisper's own default of True) for the same
        # reason as the mlx backend: conditioning feeds a hop's hallucinated
        # output back as the next hop's prompt, propagating phantoms across the
        # rolling window. Each hop carries its own window of context anyway.
        self._condition_on_previous_text = condition_on_previous_text
        self._beam_size = beam_size
        self._language = language
        self._task = task
        self._hallucination_blocklist_enabled = hallucination_blocklist_enabled
        self._hallucination_max_words = hallucination_max_words
        self._hallucination_phrases = hallucination_phrases
        # Serializes model calls across sessions/reconnects when several
        # backends share one model object (injected from
        # SharedAsrModel.inference_lock by the factory); None in tests and in
        # the inference service, which already owns a single ASR thread.
        self._inference_lock = inference_lock

    @property
    def model_name(self) -> str:
        return self._model_name

    def _ensure_model(self) -> Any:
        """Construct the CTranslate2 model on first use (lazy, then cached).

        The import and the weight download/load both happen here rather than in
        ``__init__`` so that building the pipeline factory — which happens
        unconditionally at gateway startup, before we know whether audio will
        ever arrive — stays free and dependency-light. This is the rule every
        heavy dep in this package follows, and the core container depends on it.
        """
        if self._model is None:
            from faster_whisper import WhisperModel

            # Before the model exists, so the libraries are resident by the
            # time CTranslate2 dlopens them. Skipped on an explicit "cpu",
            # where there is nothing to gain; "auto" still tries, because auto
            # means "CUDA if a GPU is visible".
            if self._device != "cpu":
                _preload_cuda_libraries()

            logger.info(
                "loading faster-whisper model %s (device=%s compute_type=%s, first use)",
                self._model_name, self._device, self._compute_type,
            )
            try:
                self._model = WhisperModel(
                    self._model_name,
                    device=self._device,
                    compute_type=self._compute_type,
                    cpu_threads=self._cpu_threads,
                )
            except Exception as exc:
                raise _cuda_library_hint(exc, self._device) from exc
            logger.info("faster-whisper model %s ready", self._model_name)
        return self._model

    def transcribe(self, pcm: bytes, sample_rate: int) -> list[Token]:
        if sample_rate != _EXPECTED_SAMPLE_RATE:
            # Whisper operates at 16 kHz and the V1 audio spec is 16 kHz, so
            # refuse rather than silently mis-transcribe a wrong-rate buffer.
            # Same contract as the other two backends.
            raise ValueError(f"expected 16 kHz PCM, got {sample_rate} Hz")
        if not pcm:
            return []

        import numpy as np

        # int16 LE bytes -> float32 in [-1, 1), the array shape
        # WhisperModel.transcribe accepts in place of a file path (its own
        # decode_audio() normalizes ffmpeg's s16le output exactly this way).
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / _INT16_FULL_SCALE

        model = self._ensure_model()

        def _run() -> list[Any]:
            segments, _info = model.transcribe(
                audio,
                word_timestamps=True,  # required: Tokens need word boundaries
                beam_size=self._beam_size,
                language=self._language,
                # "translate" always targets English, whatever was spoken.
                task=self._task,
                # NOTE the spelling: faster-whisper calls this
                # `log_prob_threshold`, where mlx-whisper calls it
                # `logprob_threshold`. Passing the mlx spelling raises
                # TypeError at runtime.
                log_prob_threshold=self._logprob_threshold,
                no_speech_threshold=self._no_speech_threshold,
                compression_ratio_threshold=self._compression_ratio_threshold,
                condition_on_previous_text=self._condition_on_previous_text,
                # faster-whisper's own Silero VAD stays off: the one VAD policy
                # in this system is StreamingTranscriber's opt-in gate, above
                # this seam, so it applies to every backend identically.
                vad_filter=False,
            )
            # `segments` is a GENERATOR — CTranslate2 runs the decode lazily as
            # it is iterated. Materializing it here (not at the call site) is
            # what makes the inference lock below actually cover the compute.
            return list(segments)

        # The CUDA hint belongs here as well as at construction, and this is
        # the site that actually catches it: CTranslate2 loads cuBLAS/cuDNN
        # LAZILY, on the first compute, not when WhisperModel is built. On a
        # CUDA-major mismatch the model therefore logs "ready" and then dies
        # inside model.encode() on the first transcribe — which is what a real
        # RTX box did (2026-09-20), sailing straight past the guard around the
        # constructor.
        try:
            if self._inference_lock is not None:
                with self._inference_lock:
                    segments = _run()
            else:
                segments = _run()
        except Exception as exc:
            raise _cuda_library_hint(exc, self._device) from exc

        tokens = _segments_to_tokens(
            segments,
            no_speech_threshold=self._no_speech_threshold,
            logprob_threshold=self._logprob_threshold,
            compression_ratio_threshold=self._compression_ratio_threshold,
            hallucination_blocklist_enabled=self._hallucination_blocklist_enabled,
            hallucination_max_words=self._hallucination_max_words,
            hallucination_phrases=self._hallucination_phrases,
        )
        logger.debug(
            "faster-whisper streaming: %d samples -> %d tokens",
            len(audio), len(tokens),
        )
        return tokens


# --- process-wide shared loader + warmup (see shared_asr_model) --------------


def load_model(
    model_name: str = DEFAULT_FASTER_WHISPER_MODEL,
    device: str = DEFAULT_DEVICE,
    compute_type: str = DEFAULT_COMPUTE_TYPE,
    cpu_threads: int = 0,
) -> Any:
    """Load a ``faster_whisper.WhisperModel``.

    Called once per process per model name by
    :func:`~openrecall_server.ingest.shared_asr_model.get_shared_model`; the
    result is injected into every per-session backend. Kept as a module-level
    function (not a method) so tests can monkeypatch it without installing
    faster-whisper.

    Unlike the MLX backends, the returned object has no thread affinity: the
    CTranslate2 model is documented as callable from several Python threads
    (``num_workers`` exists precisely to parallelize those calls), so loading it
    on one thread and inferring on another is supported.
    """
    from faster_whisper import WhisperModel

    return WhisperModel(
        model_name,
        device=device,
        compute_type=compute_type,
        cpu_threads=cpu_threads,
    )


def warmup_model(model: Any) -> None:
    """Run a warmup transcribe on 100 ms of silence so the first real hop isn't cold.

    Exercises the full convert + decode path with the model already loaded.
    Called inside ``get_shared_model``'s try/except, so a warmup failure logs
    and continues without aborting startup.
    """
    backend = FasterWhisperStreamingBackend(loaded_model=model)
    # 100 ms of 16 kHz mono silence = 1600 samples * 2 bytes = 3200 bytes.
    backend.transcribe(b"\x00" * 3200, _EXPECTED_SAMPLE_RATE)
