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
