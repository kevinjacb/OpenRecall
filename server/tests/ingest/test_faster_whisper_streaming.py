"""Tests for the portable FasterWhisperStreamingBackend (faster-whisper / CTranslate2).

This is the backend that runs on CPU *and* CUDA from one code path, so it is
the only one a non-Apple deployment can use. The unit tests never load a real
model: ``faster_whisper.WhisperModel`` is replaced by the fake below, injected
through the ``loaded_model`` seam exactly as the mlx tests inject a fake
``mlx_whisper.transcribe``.

Two properties get extra attention because they are invisible to a
"does it return tokens" test and fatal in production:

* the transcribe kwarg is spelled ``log_prob_threshold`` (faster-whisper),
  not ``logprob_threshold`` (mlx-whisper) — the mlx spelling raises TypeError
  on the real model, which a fake with ``**kwargs`` would happily swallow. A
  signature-binding test pins the whole call shape against the real API when
  faster-whisper is installed;
* ``WhisperModel.transcribe`` returns a lazy *generator*; the decode only runs
  when it is iterated, so materializing it outside the inference lock would
  leave the model call unserialized.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
import wave

import pytest

from openrecall_server.ingest.faster_whisper_streaming import (
    DEFAULT_FASTER_WHISPER_MODEL,
    FasterWhisperStreamingBackend,
    _segments_to_tokens,
    warmup_model,
)
from openrecall_server.ingest.streaming_transcriber import (
    StreamingBackend,
    Token,
    streaming_from_tokens,
)

_FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures", "audio")
_has_faster_whisper = importlib.util.find_spec("faster_whisper") is not None


# --- fakes shaped like faster_whisper.transcribe's output --------------------


class FakeWord:
    """Mimics faster_whisper.transcribe.Word (start/end in seconds)."""

    def __init__(self, word: str, start: float, end: float,
                 probability: float = 0.9) -> None:
        self.word = word
        self.start = start
        self.end = end
        self.probability = probability


class FakeSegment:
    """Mimics faster_whisper.transcribe.Segment (attributes, not dict keys)."""

    def __init__(
        self,
        text: str = "hello world",
        words: list[FakeWord] | None = None,
        no_speech_prob: float = 0.05,
        avg_logprob: float = -0.2,
        compression_ratio: float = 1.3,
        start: float = 0.0,
        end: float = 1.0,
    ) -> None:
        self.id = 0
        self.seek = 0
        self.start = start
        self.end = end
        self.text = text
        self.tokens = []
        self.avg_logprob = avg_logprob
        self.compression_ratio = compression_ratio
        self.no_speech_prob = no_speech_prob
        self.words = words
        self.temperature = 0.0


class FakeInfo:
    language = "en"
    language_probability = 0.99
    duration = 1.0


def _segment(text: str = "hello world", **kwargs) -> FakeSegment:
    """A segment with one word per space-separated token, evenly spaced.

    Mirrors the whisper tests' ``_mlx_segment`` helper; non-first words carry
    Whisper's leading-space tokenization marker.
    """
    if "words" not in kwargs and text:
        parts = text.split()
        step = 1.0 / len(parts)
        kwargs["words"] = [
            FakeWord((" " + p) if i else p, i * step, (i + 1) * step)
            for i, p in enumerate(parts)
        ]
    return FakeSegment(text=text, **kwargs)


class FakeWhisperModel:
    """Stands in for a loaded ``faster_whisper.WhisperModel``.

    ``transcribe`` returns ``(generator, info)`` — lazily, like the real one,
    so tests can observe *when* the decode happens.
    """

    def __init__(self, scripted: list[list[FakeSegment]] | None = None,
                 on_yield=None) -> None:
        self._scripted = list(scripted or [])
        self._on_yield = on_yield
        self.calls: list[tuple] = []  # (audio, kwargs)

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        segments = self._scripted.pop(0) if self._scripted else []

        def _gen():
            for seg in segments:
                if self._on_yield is not None:
                    self._on_yield()
                yield seg

        return _gen(), FakeInfo()


def make_backend(scripted=None, **kwargs):
    model = FakeWhisperModel(scripted)
    return FasterWhisperStreamingBackend(loaded_model=model, **kwargs), model


def pcm(n_samples: int = 16000, value: int = 0) -> bytes:
    return int(value).to_bytes(2, "little", signed=True) * n_samples


# --- Protocol conformance -----------------------------------------------------


def test_backend_satisfies_the_streaming_backend_protocol():
    """Substitutable for the MLX backends everywhere the wrapper is used."""
    backend, _ = make_backend()
    assert isinstance(backend, StreamingBackend)


def test_backend_can_be_wrapped_by_streaming_from_tokens():
    backend, _ = make_backend([[_segment(
        "Hello world",
        words=[FakeWord("Hello", 0.0, 0.5), FakeWord(" world", 0.5, 1.0)],
    )]])
    streamer = streaming_from_tokens(
        backend, sample_rate=16000, hop_ms=1000, window_ms=5000)

    segments = streamer.feed(pcm())

    assert [s.text for s in segments] == ["Hello world"]


# --- token conversion ---------------------------------------------------------


def test_segments_to_tokens_extracts_words_with_millisecond_timestamps():
    tokens = _segments_to_tokens([_segment(
        "hello world",
        words=[FakeWord("hello", 0.0, 0.4), FakeWord(" world", 0.5, 0.9)],
    )])
    # Seconds -> ms, and the leading space (Whisper's word-boundary marker) is
    # preserved exactly as the mlx backend preserves it.
    assert tokens == [
        Token(text="hello", start_ms=0, end_ms=400, sentence_id=1),
        Token(text=" world", start_ms=500, end_ms=900, sentence_id=1),
    ]


def test_segments_to_tokens_numbers_sentences_from_one_per_call():
    """sentence_id is the call-local segment index + 1; 0 is the sentinel for
    "no structure", so a real segment must never be stamped 0."""
    tokens = _segments_to_tokens([
        _segment("hello", words=[FakeWord("hello", 0.0, 0.5)]),
        _segment("world", words=[FakeWord(" world", 1.0, 1.5)]),
    ])
    assert [(t.text, t.sentence_id) for t in tokens] == [
        ("hello", 1), (" world", 2),
    ]


def test_segments_to_tokens_handles_words_none():
    """``Segment.words`` is None (not []) when word timestamps are missing.

    Treating it as a list would raise and take the whole hop down.
    """
    tokens = _segments_to_tokens([
        FakeSegment(text="hello", words=None),
        _segment("world", words=[FakeWord("world", 1.0, 1.5)]),
    ])
    assert [t.text for t in tokens] == ["world"]


def test_segments_to_tokens_drops_zero_length_word():
    """A word with end <= start would corrupt the committed cursor."""
    assert _segments_to_tokens([_segment(
        "hello", words=[FakeWord("hello", 0.5, 0.5)])]) == []


def test_segments_to_tokens_returns_empty_for_no_segments():
    assert _segments_to_tokens([]) == []


def test_segments_to_tokens_consumes_a_generator():
    """The real API yields segments lazily; the converter must iterate it."""
    def gen():
        yield _segment("hi", words=[FakeWord("hi", 0.0, 0.5)])

    assert [t.text for t in _segments_to_tokens(gen())] == ["hi"]


# --- the shared Whisper noise filters ------------------------------------------
# faster-whisper runs the same Whisper decoder as the mlx backend, so the same
# defenses must be in force. These assert they are actually wired, not that the
# (already-tested) helpers work.


def test_segment_above_no_speech_threshold_is_dropped():
    tokens = _segments_to_tokens(
        [_segment("hello", no_speech_prob=0.9)], no_speech_threshold=0.6)
    assert tokens == []


def test_segment_below_logprob_threshold_is_dropped():
    tokens = _segments_to_tokens(
        [_segment("hello", avg_logprob=-2.0)], logprob_threshold=-1.0)
    assert tokens == []


def test_segment_above_compression_ratio_threshold_is_dropped():
    tokens = _segments_to_tokens(
        [_segment("hello", compression_ratio=3.0)],
        compression_ratio_threshold=2.4)
    assert tokens == []


def test_short_blocklisted_phantom_phrase_is_dropped():
    """Whisper's classic near-silence phantom. Same blocklist as the mlx path."""
    assert _segments_to_tokens([_segment("Thank you.")]) == []


def test_long_sentence_containing_a_blocklisted_phrase_survives():
    text = "thank you for the coffee this morning it was great"
    tokens = _segments_to_tokens([_segment(text)])
    assert [t.text.strip() for t in tokens] == text.split()


def test_repeated_token_hallucination_is_dropped():
    word = "Congratulations"
    words = [FakeWord((" " + word) if i else word, i * 0.1, (i + 1) * 0.1)
             for i in range(6)]
    assert _segments_to_tokens([_segment(word, words=words)]) == []


def test_filters_can_be_disabled_by_the_caller():
    """The thresholds are arguments, not constants: a caller that relaxes them
    must actually see the segment."""
    tokens = _segments_to_tokens(
        [_segment("hello", no_speech_prob=0.9)], no_speech_threshold=0.95)
    assert [t.text for t in tokens] == ["hello"]


# --- the call into faster-whisper ---------------------------------------------


def test_backend_requests_word_timestamps():
    """Tokens need word boundaries; without this the backend emits nothing."""
    backend, model = make_backend([[_segment("hi")]])
    backend.transcribe(pcm(), 16000)
    assert model.calls[0][1]["word_timestamps"] is True


def test_backend_uses_faster_whispers_spelling_of_the_logprob_kwarg():
    """faster-whisper spells it ``log_prob_threshold``; mlx-whisper spells it
    ``logprob_threshold``. The mlx spelling raises TypeError on the real model,
    and a fake that accepts **kwargs would hide that — so assert the exact
    keyword the model is called with."""
    backend, model = make_backend([[_segment("hi")]], logprob_threshold=-0.8)
    backend.transcribe(pcm(), 16000)
    kwargs = model.calls[0][1]
    assert kwargs["log_prob_threshold"] == -0.8
    assert "logprob_threshold" not in kwargs


def test_backend_passes_the_noise_thresholds_through():
    backend, model = make_backend(
        [[_segment("hi")]],
        no_speech_threshold=0.42,
        compression_ratio_threshold=1.9,
    )
    backend.transcribe(pcm(), 16000)
    kwargs = model.calls[0][1]
    assert kwargs["no_speech_threshold"] == 0.42
    assert kwargs["compression_ratio_threshold"] == 1.9


def test_backend_disables_condition_on_previous_text_by_default():
    """faster-whisper defaults it to True, which feeds a hop's hallucination
    back as the next hop's prompt. The streaming default must be False."""
    backend, model = make_backend([[_segment("hi")]])
    backend.transcribe(pcm(), 16000)
    assert model.calls[0][1]["condition_on_previous_text"] is False


def test_backend_applies_the_hallucination_blocklist_to_model_output():
    """End-to-end through the backend, not just the helper: the blocklist has
    to be *wired into* transcribe(), or a phantom "Thank you." reaches the
    transcript even though the filter code is present and tested."""
    backend, _ = make_backend([[_segment("Thank you.")]])
    assert backend.transcribe(pcm(), 16000) == []


def test_backend_hallucination_knobs_reach_the_filter():
    """The constructor's blocklist knobs must be threaded through: disabling
    the blocklist has to actually let the phrase out again, and a custom phrase
    list has to be honoured."""
    off, _ = make_backend(
        [[_segment("Thank you.")]], hallucination_blocklist_enabled=False)
    assert [t.text.strip() for t in off.transcribe(pcm(), 16000)] == ["Thank", "you."]

    custom, _ = make_backend(
        [[_segment("Hello there.")]], hallucination_phrases=("hello there",))
    assert custom.transcribe(pcm(), 16000) == []


def test_backend_applies_the_confidence_gates_to_model_output():
    """Same wiring check for the no_speech / logprob / compression gates: the
    model's own thresholds only skip *decoding*, they do not stop a segment
    from being returned, so the backend must re-apply them."""
    backend, _ = make_backend(
        [[_segment("hello world", no_speech_prob=0.99)]], no_speech_threshold=0.6)
    assert backend.transcribe(pcm(), 16000) == []


def test_backend_leaves_faster_whispers_own_vad_off():
    """One VAD policy in the system: StreamingTranscriber's gate, above this
    seam. Two disagreeing VADs would silently drop speech."""
    backend, model = make_backend([[_segment("hi")]])
    backend.transcribe(pcm(), 16000)
    assert model.calls[0][1]["vad_filter"] is False


@pytest.mark.skipif(not _has_faster_whisper,
                    reason="faster-whisper not installed (the fasterwhisper extra)")
def test_call_shape_binds_against_the_real_transcribe_signature():
    """Every kwarg the backend passes must exist on the real API.

    This is the test the fake cannot be: ``FakeWhisperModel.transcribe``
    swallows **kwargs, so a typo'd or removed-in-a-future-version keyword only
    shows up on the real model. Binding against the real signature catches it
    without loading any weights.
    """
    import inspect

    from faster_whisper import WhisperModel

    backend, model = make_backend([[_segment("hi")]])
    backend.transcribe(pcm(), 16000)
    audio, kwargs = model.calls[0]
    sig = inspect.signature(WhisperModel.transcribe)
    sig.bind(None, audio, **kwargs)  # raises TypeError on an unknown kwarg


# --- audio handling -----------------------------------------------------------


def test_backend_converts_pcm_to_float32_in_minus_one_to_one():
    np = pytest.importorskip("numpy")
    backend, model = make_backend([[_segment("hi")]])
    # +16384 == half of int16 full scale -> exactly 0.5 after /32768.
    backend.transcribe(pcm(8, value=16384), 16000)
    audio = model.calls[0][0]
    assert isinstance(audio, np.ndarray)
    assert audio.dtype == np.float32
    assert len(audio) == 8
    assert audio.max() == pytest.approx(0.5)


def test_backend_rejects_non_16khz():
    backend, _ = make_backend()
    with pytest.raises(ValueError, match="16 kHz"):
        backend.transcribe(pcm(), 22050)


def test_empty_pcm_short_circuits_without_calling_the_model():
    backend, model = make_backend([[_segment("hi")]])
    assert backend.transcribe(b"", 16000) == []
    assert model.calls == []


def test_backend_returns_empty_list_for_silence():
    backend, _ = make_backend([[]])
    assert backend.transcribe(pcm(), 16000) == []


def test_backend_propagates_model_errors():
    class Boom:
        def transcribe(self, audio, **kwargs):
            raise RuntimeError("upstream timeout")

    backend = FasterWhisperStreamingBackend(loaded_model=Boom())
    with pytest.raises(RuntimeError, match="upstream timeout"):
        backend.transcribe(pcm(), 16000)


# --- the inference lock covers the decode -------------------------------------


def test_decode_runs_while_the_inference_lock_is_held():
    """``transcribe`` returns a lazy generator: the decode happens where it is
    iterated. If the backend returned the generator and converted it after
    releasing the lock, concurrent sessions would run the model unserialized.
    """
    lock = threading.Lock()
    held: list[bool] = []
    model = FakeWhisperModel(
        [[_segment("hi", words=[FakeWord("hi", 0.0, 0.5)])]],
        # locked() is True only while someone holds it; this runs as each
        # segment is pulled off the generator, i.e. during the decode.
        on_yield=lambda: held.append(lock.locked()),
    )
    backend = FasterWhisperStreamingBackend(
        loaded_model=model, inference_lock=lock)

    backend.transcribe(pcm(), 16000)

    assert held == [True], "the model decode ran outside the inference lock"
    assert not lock.locked(), "the lock was not released"


def test_works_without_an_inference_lock():
    backend, _ = make_backend([[_segment("hi", words=[FakeWord("hi", 0.0, 0.5)])]])
    assert [t.text for t in backend.transcribe(pcm(), 16000)] == ["hi"]


# --- laziness -----------------------------------------------------------------


def test_default_model_is_the_turbo_alias():
    assert DEFAULT_FASTER_WHISPER_MODEL == "large-v3-turbo"


def test_constructing_the_backend_loads_no_model():
    """The pipeline factory is built at gateway startup, before any audio: it
    must not download weights or construct a CTranslate2 model."""
    backend = FasterWhisperStreamingBackend(model="nonexistent/model")
    assert backend._model is None


def test_importing_the_module_does_not_import_faster_whisper():
    """Lazy import, the rule every heavy dep here follows — the core container
    imports this module with faster-whisper absent.

    Run in a subprocess: another test in this session may already have imported
    faster_whisper, which would make an in-process sys.modules check vacuous.
    """
    code = (
        "import sys;"
        "import openrecall_server.ingest.faster_whisper_streaming as m;"
        "m.FasterWhisperStreamingBackend();"
        "print('faster_whisper' in sys.modules or 'ctranslate2' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, check=True)
    assert out.stdout.strip() == "False", out.stdout


def test_model_is_constructed_lazily_with_device_and_compute_type(monkeypatch):
    """The device/compute_type pair is the entire portability story: it must
    reach ``WhisperModel``, and only on the first transcribe."""
    import types

    built: list[tuple] = []

    class SpyWhisperModel:
        def __init__(self, name, device=None, compute_type=None, cpu_threads=None):
            built.append((name, device, compute_type, cpu_threads))

        def transcribe(self, audio, **kwargs):
            return iter([_segment("hi", words=[FakeWord("hi", 0.0, 0.5)])]), FakeInfo()

    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = SpyWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    backend = FasterWhisperStreamingBackend(
        model="small.en", device="cuda", compute_type="float16", cpu_threads=3)
    assert built == [], "the model was constructed before any audio arrived"

    tokens = backend.transcribe(pcm(), 16000)

    assert built == [("small.en", "cuda", "float16", 3)]
    assert [t.text for t in tokens] == ["hi"]
    # Second call reuses the loaded model rather than rebuilding it.
    backend.transcribe(pcm(), 16000)
    assert len(built) == 1


def test_warmup_model_runs_one_inference_on_silence():
    model = FakeWhisperModel([[]])
    warmup_model(model)
    audio = model.calls[0][0]
    assert len(audio) == 1600  # 100 ms at 16 kHz


# --- real model (skipped unless the extra and the fixture are present) ---------


def _load_pcm(path: str) -> bytes:
    with wave.open(path, "rb") as r:
        assert r.getframerate() == 16000, f"expected 16 kHz, got {r.getframerate()}"
        return r.readframes(r.getnframes())


@pytest.mark.skipif(
    not _has_faster_whisper
    or not os.path.exists(os.path.join(_FIXTURES, "speaker_a_1.wav")),
    reason="fasterwhisper extra or the audio fixture is not present",
)
def test_real_model_transcribes_the_fixture():
    """The only automated proof that the whole path — float32 conversion,
    word timestamps, Token conversion — produces real words from real audio.

    Uses a small model on CPU int8 so it is a test, not a benchmark; the
    fixture is LibriSpeech, whose first sentence is about "Mr. Quilter".
    """
    backend = FasterWhisperStreamingBackend(
        model="tiny.en", device="cpu", compute_type="int8")
    tokens = backend.transcribe(
        _load_pcm(os.path.join(_FIXTURES, "speaker_a_1.wav")), 16000)

    text = " ".join(t.text.strip() for t in tokens).lower()
    assert "quilter" in text, text
    # Word timestamps must be real, ascending and inside the 4 s clip.
    assert all(t.end_ms > t.start_ms for t in tokens)
    assert [t.start_ms for t in tokens] == sorted(t.start_ms for t in tokens)
    assert max(t.end_ms for t in tokens) <= 10_000


# --- CUDA library diagnostics -------------------------------------------------
# CTranslate2 wheels are built against one CUDA major version; a CUDA 13 host
# hits "Library libcublas.so.12 is not found or cannot be loaded", which names
# a file and not the cause. Hit for real on an RTX box, 2026-09-20.

def test_a_missing_cuda_library_explains_the_major_version_mismatch():
    from openrecall_server.ingest.faster_whisper_streaming import _cuda_library_hint

    out = str(_cuda_library_hint(
        RuntimeError("Library libcublas.so.12 is not found or cannot be loaded"),
        "cuda"))
    assert "libcublas.so.12" in out, "the original error must survive"
    assert "nvidia-cublas-cu12" in out and "LD_LIBRARY_PATH" in out, (
        "the hint must carry the actual fix, not just a diagnosis")
    assert "do NOT downgrade" in out, (
        "downgrading a working CUDA 13 toolkit is the wrong instinct this "
        "message exists to head off")


def test_a_cudnn_error_is_recognised_too():
    from openrecall_server.ingest.faster_whisper_streaming import _cuda_library_hint

    out = str(_cuda_library_hint(
        RuntimeError("Library libcudnn_ops.so.9 is not found"), "cuda"))
    assert "nvidia-cudnn-cu12" in out


def test_an_unrelated_cuda_failure_is_passed_through_untouched():
    """A wrong guess buries the real error under a confident, irrelevant
    suggestion — worse than saying nothing."""
    from openrecall_server.ingest.faster_whisper_streaming import _cuda_library_hint

    original = RuntimeError("CUDA failed with error out of memory")
    assert _cuda_library_hint(original, "cuda") is original


def test_the_hint_never_fires_on_cpu():
    """On CPU a libcublas message cannot mean a CUDA-major mismatch, so the
    advice would be nonsense."""
    from openrecall_server.ingest.faster_whisper_streaming import _cuda_library_hint

    original = RuntimeError("Library libcublas.so.12 is not found")
    assert _cuda_library_hint(original, "cpu") is original


def test_the_cuda_extra_pins_the_libraries_the_hint_tells_you_to_install():
    """The message and the extra must not drift: if someone renames a package
    in pyproject, the advice silently becomes wrong."""
    import tomllib

    with open("pyproject.toml", "rb") as fh:
        extras = tomllib.load(fh)["project"]["optional-dependencies"]
    cuda = " ".join(extras["cuda"])
    for pkg in ("nvidia-cublas-cu12", "nvidia-cudnn-cu12"):
        assert pkg in cuda, f"{pkg} is named in the hint but not in the cuda extra"
    assert "sys_platform == 'linux'" in cuda, (
        "these wheels are Linux-only; without a marker, installing the extra "
        "on a Mac fails")


def test_the_model_load_path_actually_applies_the_hint(monkeypatch):
    """The wiring, not the helper.

    Testing _cuda_library_hint alone proves nothing about the code path an
    operator hits: removing the try/except around WhisperModel(...) leaves
    every direct-call test green while the real failure reverts to the bare
    "libcublas.so.12 is not found". That is the same shape as the blocklist
    gap an earlier review caught here — a helper verified in isolation and
    never checked at its call site.
    """
    import faster_whisper

    def boom(*args, **kwargs):
        raise RuntimeError("Library libcublas.so.12 is not found or cannot be loaded")

    monkeypatch.setattr(faster_whisper, "WhisperModel", boom)
    backend = FasterWhisperStreamingBackend(device="cuda")

    with pytest.raises(RuntimeError) as excinfo:
        backend.transcribe(b"\x00" * 3200, 16000)

    message = str(excinfo.value)
    assert "nvidia-cublas-cu12" in message, (
        "the load path raised the bare CTranslate2 error; the hint is not wired in")
    assert "libcublas.so.12" in message, "the original cause must survive"


def test_the_hint_also_covers_the_first_inference_not_just_the_load(monkeypatch):
    """CTranslate2 loads cuBLAS/cuDNN LAZILY, on first compute.

    On a CUDA-major mismatch the model therefore logs "ready" and dies inside
    model.encode() on the first transcribe, sailing straight past a guard that
    only wraps the constructor. Observed on a real RTX box, 2026-09-20: the
    log read "faster-whisper model large-v3-turbo ready" one line above the
    libcublas traceback.
    """
    class LazilyFailingModel:
        def transcribe(self, *args, **kwargs):
            raise RuntimeError("Library libcublas.so.12 is not found or cannot be loaded")

    backend = FasterWhisperStreamingBackend(
        loaded_model=LazilyFailingModel(), device="cuda")

    with pytest.raises(RuntimeError) as excinfo:
        backend.transcribe(b"\x00" * 3200, 16000)

    message = str(excinfo.value)
    assert "nvidia-cublas-cu12" in message, (
        "the inference path raised the bare CTranslate2 error; the model loads "
        "fine and only fails at first compute, so guarding the constructor "
        "alone misses the case operators actually hit")
    assert "libcublas.so.12" in message


def test_a_real_inference_error_is_not_dressed_up_as_a_cuda_problem():
    """Guarding the whole inference call must not relabel ordinary failures."""
    class Boom:
        def transcribe(self, *args, **kwargs):
            raise ValueError("bad audio shape")

    backend = FasterWhisperStreamingBackend(loaded_model=Boom(), device="cuda")
    with pytest.raises(ValueError, match="bad audio shape"):
        backend.transcribe(b"\x00" * 3200, 16000)


def test_webrtcvad_consumers_declare_setuptools():
    """webrtcvad does `import pkg_resources` at module scope and Python 3.12+
    venvs ship no setuptools, so a fresh install of either extra dies at
    runtime. Hit on a real box (2026-09-20) after being fixed only in the
    Docker image — a machine that happens to have setuptools hides it."""
    import tomllib

    with open("pyproject.toml", "rb") as fh:
        extras = tomllib.load(fh)["project"]["optional-dependencies"]
    for name in ("speaker", "vad"):
        assert any(d.split(";")[0].strip().startswith("setuptools")
                   for d in extras[name]), (
            f"the {name} extra reaches webrtcvad but does not declare setuptools")


# --- NVIDIA runtime discovery / preload ---------------------------------------

def _fake_nvidia_tree(tmp_path):
    """A PEP 420 namespace package, exactly like the real nvidia-*-cu12 wheels:
    no __init__.py anywhere, so every __file__ in the tree is None."""
    for sub in ("cublas", "cudnn"):
        d = tmp_path / "nvidia" / sub / "lib"
        d.mkdir(parents=True)
        (d / f"lib{sub}.so.12").write_bytes(b"not a real shared object")
    return tmp_path


def test_nvidia_lib_dirs_works_on_namespace_packages(tmp_path, monkeypatch):
    """The bug this exists for.

    nvidia.cublas.lib.__file__ is None for a namespace package, so the
    os.path.dirname(...__file__) recipe in faster-whisper's own docs — which I
    shipped verbatim — raises TypeError instead of printing a path. Hit on a
    real box, 2026-09-20.
    """
    import sys

    from openrecall_server.ingest import faster_whisper_streaming as fw

    monkeypatch.syspath_prepend(str(_fake_nvidia_tree(tmp_path)))
    for mod in [m for m in sys.modules if m == "nvidia" or m.startswith("nvidia.")]:
        monkeypatch.delitem(sys.modules, mod, raising=False)

    import nvidia
    assert nvidia.__file__ is None, "fixture is not a namespace package"

    dirs = fw._nvidia_lib_dirs()
    assert len(dirs) == 2, dirs
    assert all(d.endswith("/lib") for d in dirs)


def test_nvidia_lib_dirs_is_empty_when_nothing_is_installed(monkeypatch):
    """A CPU box has no nvidia package; discovery must be silent, not an error."""
    import builtins
    import sys

    from openrecall_server.ingest import faster_whisper_streaming as fw

    for mod in [m for m in sys.modules if m == "nvidia" or m.startswith("nvidia.")]:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    real_import = builtins.__import__

    def no_nvidia(name, *args, **kwargs):
        if name == "nvidia":
            raise ImportError("No module named 'nvidia'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_nvidia)
    assert fw._nvidia_lib_dirs() == []


def test_preload_never_raises_on_libraries_it_cannot_load(tmp_path, monkeypatch):
    """Best effort by design: a file that is not a loadable shared object (wrong
    arch, truncated, or a stub) must not take the service down — the model load
    still fails afterwards with the explanatory hint."""
    import sys

    from openrecall_server.ingest import faster_whisper_streaming as fw

    monkeypatch.syspath_prepend(str(_fake_nvidia_tree(tmp_path)))
    for mod in [m for m in sys.modules if m == "nvidia" or m.startswith("nvidia.")]:
        monkeypatch.delitem(sys.modules, mod, raising=False)

    assert fw._preload_cuda_libraries() == []   # nothing loadable, no exception


def test_the_hint_does_not_ship_the_broken_dirname_recipe():
    """Guard against reintroducing it: os.path.dirname(x.__file__) is exactly
    what fails on the namespace-package layout."""
    from openrecall_server.ingest.faster_whisper_streaming import _cuda_library_hint

    msg = str(_cuda_library_hint(RuntimeError("libcublas.so.12 missing"), "cuda"))
    assert "os.path.dirname(nvidia" not in msg, (
        "the hint suggests the __file__ recipe, which raises TypeError on the "
        "real wheels")
    assert "__path__" in msg


def test_the_hints_shell_snippet_is_runnable_python():
    """The advice has to execute. The version shipped before this crashed with
    TypeError the moment anyone pasted it."""
    import ast
    import re

    from openrecall_server.ingest.faster_whisper_streaming import _cuda_library_hint

    msg = str(_cuda_library_hint(RuntimeError("libcublas.so.12 missing"), "cuda"))
    snippet = re.search(r'python -c "(.+?)"', msg, re.S)
    assert snippet, "no python -c snippet found in the hint"
    ast.parse(snippet.group(1))       # raises SyntaxError if malformed


def test_preload_is_skipped_on_an_explicit_cpu_device(monkeypatch):
    """Nothing to gain, and walking site-packages on every CPU box is waste."""
    from openrecall_server.ingest import faster_whisper_streaming as fw

    calls = []
    monkeypatch.setattr(fw, "_preload_cuda_libraries", lambda: calls.append(1) or [])

    class Model:
        def transcribe(self, *a, **k):
            return iter(()), None

    monkeypatch.setattr(
        "faster_whisper.WhisperModel", lambda *a, **k: Model(), raising=False)

    fw.FasterWhisperStreamingBackend(device="cpu").transcribe(b"\x00" * 3200, 16000)
    assert calls == [], "preload ran on an explicit cpu device"

    fw.FasterWhisperStreamingBackend(device="cuda").transcribe(b"\x00" * 3200, 16000)
    assert calls == [1], "preload did not run on cuda"


# --- language + task (non-English wearers) ------------------------------------

def test_the_task_reaches_the_model():
    """Whisper's "translate" task emits English whatever was spoken, which is
    how a non-English wearer keeps one language downstream — extraction,
    embeddings, retrieval and the agent all reason in English. The backend
    previously never passed `task` at all, so translation was unreachable."""
    backend, model = make_backend(task="translate", language="ml")
    backend.transcribe(pcm(3200), 16000)
    _audio, kwargs = model.calls[0]
    assert kwargs["task"] == "translate"
    assert kwargs["language"] == "ml"


def test_transcribe_is_the_default_task():
    backend, model = make_backend()
    backend.transcribe(pcm(3200), 16000)
    _audio, kwargs = model.calls[0]
    assert kwargs["task"] == "transcribe"
    assert kwargs["language"] is None, "unset means detect"
