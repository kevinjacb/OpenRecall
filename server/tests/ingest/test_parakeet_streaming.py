"""Tests for the Parakeet streaming backend.

The backend is the alternative to WhisperStreamingBackend behind the same
StreamingBackend Protocol. Everything heavy (mlx, numpy, parakeet_mlx) is
lazy-imported behind the ``model`` / ``featurize`` seams, so this suite runs
with none of them installed — the fakes below stand in for the real model.

The behavior that matters most here is sub-word regrouping: parakeet-mlx emits
sentencepiece pieces meant to be joined with "" (the leading space IS the word
boundary), while StreamingTranscriber joins its tokens with " ". Emitting raw
pieces upward would yield "Hel lo wor ld", so the backend merges pieces into
whole words first.
"""

import pytest

from openrecall_server.ingest.parakeet_streaming import (
    DEFAULT_PARAKEET_MODEL,
    ParakeetStreamingBackend,
    _aligned_tokens_to_tokens,
)
from openrecall_server.ingest.streaming_transcriber import (
    StreamingBackend,
    Token,
    streaming_from_tokens,
)


class FakeAlignedToken:
    """Mimics parakeet_mlx.AlignedToken (text/start/duration/end, seconds)."""

    def __init__(self, text: str, start: float, end: float) -> None:
        self.text = text
        self.start = start
        self.duration = end - start
        self.end = end


class FakeSentence:
    def __init__(self, tokens):
        self.tokens = tokens


class FakeResult:
    """Mimics AlignedResult: sentences + a flattened `tokens` property."""

    def __init__(self, tokens):
        self.sentences = [FakeSentence(tokens)]

    @property
    def tokens(self):
        return [t for s in self.sentences for t in s.tokens]


class FakeModel:
    """Stands in for a loaded ParakeetTDT. Records the mel it was handed."""

    def __init__(self, results_per_call):
        self._results = list(results_per_call)
        self.calls = []

    def generate(self, mel):
        self.calls.append(mel)
        if not self._results:
            return []
        return self._results.pop(0)


def fake_featurize(pcm: bytes, sample_rate: int):
    """Stand-in for the numpy/mlx get_logmel chain."""
    return ("mel", len(pcm))


def make_backend(results_per_call):
    model = FakeModel(results_per_call)
    return ParakeetStreamingBackend(model=model, featurize=fake_featurize), model


def pcm(n_samples: int = 16000) -> bytes:
    return b"\x00\x01" * n_samples


# --- Protocol conformance ----------------------------------------------------


def test_backend_satisfies_the_streaming_backend_protocol():
    """The whole point of the switch: Parakeet is substitutable for Whisper
    everywhere the streaming wrapper is used."""
    backend, _ = make_backend([])
    assert isinstance(backend, StreamingBackend)


def test_backend_can_be_wrapped_by_streaming_from_tokens():
    words = [FakeAlignedToken("Hello", 0.0, 0.5), FakeAlignedToken(" world", 0.5, 1.0)]
    backend, _ = make_backend([[FakeResult(words)]])
    streamer = streaming_from_tokens(backend, sample_rate=16000, hop_ms=1000, window_ms=5000)

    segments = streamer.feed(pcm())

    assert [s.text for s in segments] == ["Hello world"]


# --- sub-word regrouping -----------------------------------------------------


def test_subword_pieces_are_merged_into_whole_words():
    """Parakeet emits sentencepiece pieces; a leading space starts a new word."""
    pieces = [
        FakeAlignedToken("Hel", 0.0, 0.2),
        FakeAlignedToken("lo", 0.2, 0.4),
        FakeAlignedToken(" wor", 0.4, 0.7),
        FakeAlignedToken("ld", 0.7, 0.9),
    ]

    tokens = _aligned_tokens_to_tokens(pieces)

    assert tokens == [
        Token(text="Hello", start_ms=0, end_ms=400),
        Token(text="world", start_ms=400, end_ms=900),
    ]


def test_merged_words_join_to_correct_text_through_the_wrapper():
    """End-to-end guard against the ' '.join vs ''.join mismatch: the wrapper
    joins with a space, so the backend must hand it whole, stripped words."""
    pieces = [
        FakeAlignedToken("Book", 0.0, 0.3),
        FakeAlignedToken(" the", 0.3, 0.5),
        FakeAlignedToken(" flight", 0.5, 1.0),
    ]
    backend, _ = make_backend([[FakeResult(pieces)]])
    streamer = streaming_from_tokens(backend, sample_rate=16000, hop_ms=1000, window_ms=5000)

    segments = streamer.feed(pcm())

    assert [s.text for s in segments] == ["Book the flight"]


def test_word_timestamps_span_first_to_last_piece():
    pieces = [
        FakeAlignedToken("un", 1.0, 1.2),
        FakeAlignedToken("believ", 1.2, 1.6),
        FakeAlignedToken("able", 1.6, 2.0),
    ]

    tokens = _aligned_tokens_to_tokens(pieces)

    assert tokens == [Token(text="unbelievable", start_ms=1000, end_ms=2000)]


def test_zero_length_word_is_dropped():
    """A fully collapsed word would confuse the wrapper's committed cursor."""
    assert _aligned_tokens_to_tokens([FakeAlignedToken(" x", 1.0, 1.0)]) == []


def test_empty_and_whitespace_pieces_are_ignored():
    pieces = [
        FakeAlignedToken("", 0.0, 0.1),
        FakeAlignedToken("hi", 0.0, 0.4),
    ]
    assert _aligned_tokens_to_tokens(pieces) == [
        Token(text="hi", start_ms=0, end_ms=400)
    ]


def test_negative_timestamps_are_clamped_to_zero():
    tokens = _aligned_tokens_to_tokens([FakeAlignedToken("a", -1.0, 0.5)])
    assert tokens == [Token(text="a", start_ms=0, end_ms=500)]


# --- silence / empty handling ------------------------------------------------


def test_silent_hop_yields_no_tokens():
    """The reason this backend exists: the transducer emits blanks on silence,
    so an empty result must map to zero tokens (not a phantom phrase)."""
    backend, _ = make_backend([[FakeResult([])]])
    assert backend.transcribe(pcm(), 16000) == []


def test_generate_returning_empty_list_yields_no_tokens():
    backend, _ = make_backend([[]])
    assert backend.transcribe(pcm(), 16000) == []


def test_empty_pcm_short_circuits_without_calling_the_model():
    backend, model = make_backend([[FakeResult([FakeAlignedToken("x", 0, 1)])]])
    assert backend.transcribe(b"", 16000) == []
    assert model.calls == []


def test_result_without_tokens_property_falls_back_to_sentences():
    class NoTokensProperty:
        def __init__(self, tokens):
            self.sentences = [FakeSentence(tokens)]

    backend, _ = make_backend(
        [[NoTokensProperty([FakeAlignedToken("hey", 0.0, 0.5)])]]
    )
    assert backend.transcribe(pcm(), 16000) == [
        Token(text="hey", start_ms=0, end_ms=500)
    ]


# --- contract guards ---------------------------------------------------------


def test_wrong_sample_rate_raises():
    backend, _ = make_backend([])
    with pytest.raises(ValueError, match="16 kHz"):
        backend.transcribe(pcm(), 8000)


def test_featurizer_receives_the_pcm_and_rate():
    seen = {}

    def spy(p, sr):
        seen["bytes"] = len(p)
        seen["rate"] = sr
        return "mel"

    model = FakeModel([[FakeResult([])]])
    backend = ParakeetStreamingBackend(model=model, featurize=spy)
    backend.transcribe(pcm(800), 16000)

    assert seen == {"bytes": 1600, "rate": 16000}
    assert model.calls == ["mel"]


def test_default_model_name_is_the_tdt_v3_repo():
    assert DEFAULT_PARAKEET_MODEL == "mlx-community/parakeet-tdt-0.6b-v3"


def test_constructing_the_backend_does_not_load_a_model():
    """Building the pipeline factory happens at gateway startup, before any
    audio arrives; it must not import parakeet_mlx or download weights."""
    backend = ParakeetStreamingBackend(model_name="nonexistent/repo")
    assert backend._model is None
