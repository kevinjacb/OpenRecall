"""Tests for the production WhisperStreamingBackend.

The backend wraps mlx-whisper with ``word_timestamps=True`` and
returns a list of :class:`~sense_server.ingest.streaming_transcriber.Token`
suitable for the streaming transcriber to consume. The tests use a
fake mlx_whisper (no MLX dependency in the unit test path) that
returns a dict shaped like mlx-whisper's real output.
"""
from __future__ import annotations

import pytest

from sense_server.ingest.streaming_transcriber import Token
from sense_server.ingest.whisper_streaming import (
    WhisperStreamingBackend,
    _mlx_segments_to_tokens,
    _seconds_to_ms,
)


# --- helpers -----------------------------------------------------------------


def _mlx_segment(
    text: str = "hello world",
    words: list[dict] | None = None,
    start: float = 0.0,
    end: float | None = None,
) -> dict:
    """Build a dict shaped like one entry in mlx-whisper's ``segments`` list."""
    if words is None and text:
        # Default: one token per space-separated word, evenly spaced
        tokens = text.split()
        if len(tokens) > 1:
            dur = (end or 1.0) - start
            step = dur / len(tokens)
            words = [
                {"word": " " + t if i > 0 else t, "start": start + i * step, "end": start + (i + 1) * step}
                for i, t in enumerate(tokens)
            ]
        else:
            words = [{"word": tokens[0], "start": start, "end": end or start + 0.5}]
    return {"text": text, "start": start, "end": end, "words": words or []}


def _make_mlx_response(*segments: dict) -> dict:
    return {"text": " ".join(s["text"] for s in segments), "segments": list(segments)}


class FakeMlxWhisper:
    """A fake mlx_whisper.transcribe() that returns a scripted response.

    Mirrors the real signature: ``transcribe(audio, path_or_hf_repo, **kwargs)``.
    """

    def __init__(self, scripted: list[dict]) -> None:
        self._scripted = list(scripted)
        self._idx = 0
        self.calls: list[tuple[Any, str, dict]] = []  # (audio, model, kwargs)

    def __call__(self, audio, path_or_hf_repo: str, **kwargs):
        self.calls.append((audio, path_or_hf_repo, kwargs))
        if self._idx >= len(self._scripted):
            return {"text": "", "segments": []}
        out = self._scripted[self._idx]
        self._idx += 1
        return out


# --- unit tests for the pure helpers ----------------------------------------


def test_seconds_to_ms_rounds_to_nearest():
    assert _seconds_to_ms(0.0) == 0
    assert _seconds_to_ms(0.4) == 400
    assert _seconds_to_ms(1.234) == 1234
    assert _seconds_to_ms(0.0005) == 0   # rounds down
    assert _seconds_to_ms(0.0015) == 2  # rounds up


def test_mlx_segments_to_tokens_extracts_words_with_timestamps():
    response = _make_mlx_response(_mlx_segment(
        text="hello world",
        words=[
            {"word": "hello", "start": 0.0, "end": 0.4},
            {"word": " world", "start": 0.5, "end": 0.9},
        ],
    ))
    tokens = _mlx_segments_to_tokens(response)
    # mlx-whisper prefixes word-tokens with " " as a tokenization marker;
    # the streaming wrapper joins with "" (empty string) so the leading
    # space IS the word boundary. We preserve it here.
    assert tokens == [
        Token(text="hello", start_ms=0, end_ms=400),
        Token(text=" world", start_ms=500, end_ms=900),
    ]


def test_mlx_segments_to_tokens_flattens_multiple_segments():
    response = _make_mlx_response(
        _mlx_segment(text="hello", words=[{"word": "hello", "start": 0.0, "end": 0.5}]),
        _mlx_segment(text="world", words=[{"word": " world", "start": 1.0, "end": 1.5}]),
    )
    tokens = _mlx_segments_to_tokens(response)
    assert [t.text for t in tokens] == ["hello", " world"]
    assert [t.start_ms for t in tokens] == [0, 1000]


def test_mlx_segments_to_tokens_skips_silence_with_no_words():
    """A segment with an empty `words` array represents silence — skip it."""
    response = _make_mlx_response(
        _mlx_segment(text="", words=[]),
        _mlx_segment(
            text="hello",
            words=[{"word": "hello", "start": 0.5, "end": 0.9}],
        ),
    )
    tokens = _mlx_segments_to_tokens(response)
    assert [t.text for t in tokens] == ["hello"]


def test_mlx_segments_to_tokens_returns_empty_for_no_segments():
    response = {"text": "", "segments": []}
    assert _mlx_segments_to_tokens(response) == []


def test_mlx_segments_to_tokens_preserves_leading_space_as_word_boundary():
    """The leading space on a non-first token IS the word boundary.

    Stripping it would glue words together; preserving it lets the
    streaming wrapper join with ``""`` and reconstruct the original
    transcript. This is the convention mlx-whisper uses internally.
    """
    response = _make_mlx_response(_mlx_segment(
        text="hello",
        words=[{"word": " hello", "start": 0.0, "end": 0.4}],
    ))
    tokens = _mlx_segments_to_tokens(response)
    assert tokens[0].text == " hello"


def test_mlx_segments_to_tokens_rejects_invalid_timestamps():
    """A word with start > end is a sign of malformed output. Drop it."""
    response = _make_mlx_response(_mlx_segment(
        text="hello",
        words=[{"word": "hello", "start": 0.5, "end": 0.3}],
    ))
    assert _mlx_segments_to_tokens(response) == []


# --- tests for the backend itself ---------------------------------------------


def test_backend_uses_default_model():
    fake = FakeMlxWhisper([_make_mlx_response(_mlx_segment(text="hi"))])
    b = WhisperStreamingBackend(mlx_transcribe=fake)
    b.transcribe(b"\x00" * 16000, 16000)
    assert fake.calls[0][1] == "mlx-community/whisper-large-v3-turbo"


def test_backend_uses_custom_model_when_specified():
    fake = FakeMlxWhisper([_make_mlx_response(_mlx_segment(text="hi"))])
    b = WhisperStreamingBackend(mlx_transcribe=fake, model="mlx-community/whisper-small")
    b.transcribe(b"\x00" * 16000, 16000)
    assert fake.calls[0][1] == "mlx-community/whisper-small"


def test_backend_rejects_non_16khz():
    fake = FakeMlxWhisper([])
    b = WhisperStreamingBackend(mlx_transcribe=fake)
    with pytest.raises(ValueError, match="16 kHz"):
        b.transcribe(b"\x00" * 16000, 22050)


def test_backend_converts_pcm_bytes_to_float32_array():
    """The backend converts int16 LE bytes to a float32 numpy array
    in [-1, 1) before calling mlx-whisper."""
    fake = FakeMlxWhisper([_make_mlx_response(_mlx_segment(text="hi"))])
    b = WhisperStreamingBackend(mlx_transcribe=fake)
    pcm = b"\x00" * 16000
    b.transcribe(pcm, 16000)
    audio_arg = fake.calls[0][0]
    # The audio should be a numpy float32 array, length 8000 (16kHz mono).
    import numpy as np
    assert isinstance(audio_arg, np.ndarray)
    assert audio_arg.dtype == np.float32
    assert len(audio_arg) == 8000


def test_backend_returns_tokens_for_words():
    fake = FakeMlxWhisper([_make_mlx_response(_mlx_segment(
        text="hello world",
        words=[
            {"word": "hello", "start": 0.0, "end": 0.4},
            {"word": " world", "start": 0.5, "end": 0.9},
        ],
    ))])
    b = WhisperStreamingBackend(mlx_transcribe=fake)
    tokens = b.transcribe(b"\x00" * 16000, 16000)
    assert tokens == [
        Token(text="hello", start_ms=0, end_ms=400),
        Token(text=" world", start_ms=500, end_ms=900),
    ]


def test_backend_returns_empty_list_for_silence():
    fake = FakeMlxWhisper([_make_mlx_response()])
    b = WhisperStreamingBackend(mlx_transcribe=fake)
    assert b.transcribe(b"\x00" * 16000, 16000) == []


def test_backend_passes_word_timestamps_true():
    """The whole point of the streaming backend is word-level timestamps.
    Confirm the call to mlx-whisper enables them.
    """
    fake = FakeMlxWhisper([_make_mlx_response(_mlx_segment(text="x"))])
    b = WhisperStreamingBackend(mlx_transcribe=fake)
    b.transcribe(b"\x00" * 16000, 16000)
    assert fake.calls[0][2]["word_timestamps"] is True


def test_backend_propagates_mlx_whisper_errors():
    class Boom:
        def __call__(self, audio, path_or_hf_repo, **kwargs):
            raise RuntimeError("upstream timeout")

    b = WhisperStreamingBackend(mlx_transcribe=Boom())
    with pytest.raises(RuntimeError, match="upstream timeout"):
        b.transcribe(b"\x00" * 16000, 16000)


def test_backend_handles_real_whisper_output_shape():
    """A realistic mlx-whisper response with a single segment, multiple
    words, including common artefacts (leading spaces, mid-sentence
    punctuation) should produce clean tokens.
    """
    response = _make_mlx_response({
        "text": " Hello, world! How are you?",
        "start": 0.0,
        "end": 2.5,
        "words": [
            {"word": " Hello,", "start": 0.0, "end": 0.5},
            {"word": " world!", "start": 0.6, "end": 1.0},
            {"word": " How", "start": 1.2, "end": 1.5},
            {"word": " are", "start": 1.5, "end": 1.8},
            {"word": " you?", "start": 1.8, "end": 2.2},
        ],
    })
    fake = FakeMlxWhisper([response])
    b = WhisperStreamingBackend(mlx_transcribe=fake)
    tokens = b.transcribe(b"\x00" * 16000, 16000)
    # mlx-whisper's first word-token often has a leading space
    # (the segment-text concat pattern is: words[0] + words[1] + ...,
    # where words[1..] have a leading " " that joins cleanly when
    # the consumer concatenates without spaces). The streaming
    # wrapper joins tokens with "" (empty string) so the leading
    # space is the word boundary. We preserve it here as-is.
    assert [t.text for t in tokens] == [" Hello,", " world!", " How", " are", " you?"]
    assert [t.start_ms for t in tokens] == [0, 600, 1200, 1500, 1800]
    assert [t.end_ms for t in tokens] == [500, 1000, 1500, 1800, 2200]
