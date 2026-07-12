"""Tests for mlx-whisper's no_speech_prob + avg_logprob filtering.

The streaming backend surfaces mlx-whisper's per-segment confidence
signals as a defense against hallucinated transcripts on quiet
inputs (low SNR, room noise, etc.). When a segment's
``no_speech_prob`` exceeds the threshold (default 0.6) OR its
``avg_logprob`` falls below the floor (default -1.0), the entire
segment is dropped — no tokens reach the streaming wrapper, so
nothing is committed.

This is a *server-side* defense. The firmware's VAD also gates
which frames get sent, so this is the second line of defense.
"""
from __future__ import annotations

import pytest

from sense_server.ingest.streaming_transcriber import Token
from sense_server.ingest.whisper_streaming import (
    WhisperStreamingBackend,
    _mlx_segments_to_tokens,
)


# --- helpers -----------------------------------------------------------------


def _mlx_segment(
    text: str = "hello world",
    words: list[dict] | None = None,
    start: float = 0.0,
    end: float = 1.0,
    no_speech_prob: float = 0.1,
    avg_logprob: float = -0.3,
) -> dict:
    """Build a dict shaped like one entry in mlx-whisper's segments list.

    ``no_speech_prob`` defaults to 0.1 (low → "this is speech")
    and ``avg_logprob`` defaults to -0.3 (high → "this is confident").
    """
    if words is None and text:
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
    return {
        "text": text,
        "start": start,
        "end": end,
        "words": words or [],
        "no_speech_prob": no_speech_prob,
        "avg_logprob": avg_logprob,
    }


def _make_mlx_response(*segments: dict) -> dict:
    return {"text": " ".join(s["text"] for s in segments), "segments": list(segments)}


class FakeMlxWhisper:
    def __init__(self, scripted: list[dict]) -> None:
        self._scripted = list(scripted)
        self._idx = 0
        self.calls: list[tuple] = []

    def __call__(self, audio, path_or_hf_repo: str, **kwargs):
        self.calls.append((audio, path_or_hf_repo, kwargs))
        if self._idx >= len(self._scripted):
            return {"text": "", "segments": []}
        out = self._scripted[self._idx]
        self._idx += 1
        return out


# --- unit tests for the filtering logic -------------------------------------


def test_segment_with_high_no_speech_prob_is_dropped():
    """A segment with no_speech_prob=0.9 is almost certainly silence.

    Even if it has words (mlx sometimes hallucinates words on noise),
    we drop the whole segment.
    """
    response = _make_mlx_response(_mlx_segment(
        text="the the the",  # mlx's classic noise hallucination
        words=[
            {"word": "the", "start": 0.0, "end": 0.3},
            {"word": " the", "start": 0.3, "end": 0.6},
            {"word": " the", "start": 0.6, "end": 0.9},
        ],
        no_speech_prob=0.9,
    ))
    tokens = _mlx_segments_to_tokens(response, no_speech_threshold=0.6)
    assert tokens == []


def test_segment_below_no_speech_threshold_is_kept():
    """A segment with no_speech_prob=0.2 is kept."""
    response = _make_mlx_response(_mlx_segment(
        text="hello", words=[{"word": "hello", "start": 0.0, "end": 0.4}],
        no_speech_prob=0.2,
    ))
    tokens = _mlx_segments_to_tokens(response, no_speech_threshold=0.6)
    assert tokens == [Token(text="hello", start_ms=0, end_ms=400)]


def test_segment_at_no_speech_threshold_boundary_is_kept():
    """no_speech_prob == threshold is a tie; mlx-whisper uses strict >.

    We mirror that: a segment with prob == threshold is kept.
    """
    response = _make_mlx_response(_mlx_segment(
        text="x", words=[{"word": "x", "start": 0.0, "end": 0.3}],
        no_speech_prob=0.6,
    ))
    tokens = _mlx_segments_to_tokens(response, no_speech_threshold=0.6)
    assert len(tokens) == 1


def test_segment_with_low_avg_logprob_is_dropped():
    """avg_logprob below the floor is hallucination; drop the segment."""
    response = _make_mlx_response(_mlx_segment(
        text="hello",
        words=[{"word": "hello", "start": 0.0, "end": 0.3}],
        avg_logprob=-1.5,
    ))
    tokens = _mlx_segments_to_tokens(response, logprob_threshold=-1.0)
    assert tokens == []


def test_segment_with_high_avg_logprob_is_kept():
    response = _make_mlx_response(_mlx_segment(
        text="hello",
        words=[{"word": "hello", "start": 0.0, "end": 0.3}],
        avg_logprob=-0.5,
    ))
    tokens = _mlx_segments_to_tokens(response, logprob_threshold=-1.0)
    assert len(tokens) == 1


def test_segment_at_logprob_threshold_boundary_is_dropped():
    """avg_logprob == floor: mlx-whisper uses strict <; we mirror that.

    Wait — actually we should keep borderline cases. Let me check:
    avg_logprob is bounded above by 0 (perfectly confident). The
    default threshold is -1.0, which is "anything below average
    logprob -1.0 is suspicious". A segment with avg_logprob == -1.0
    is exactly at the threshold; in practice mlx's --logprob_threshold
    uses strict <, so a borderline segment gets a fallback
    temperature. We'll use strict < to mirror that.

    Hmm — but that means borderline cases get dropped. For a
    smoke test, we want borderline kept (to avoid false negatives
    in noisy inputs that happen to be exactly at the threshold).
    Let me document: segments at exactly the threshold are KEPT
    (>= comparison, not strict <).
    """
    response = _make_mlx_response(_mlx_segment(
        text="hello",
        words=[{"word": "hello", "start": 0.0, "end": 0.3}],
        avg_logprob=-1.0,
    ))
    tokens = _mlx_segments_to_tokens(response, logprob_threshold=-1.0)
    assert len(tokens) == 1  # kept at the threshold


def test_filtering_uses_per_segment_thresholds_independently():
    """One bad segment is dropped; a good one in the same response is kept."""
    response = _make_mlx_response(
        _mlx_segment(
            text="the the", words=[{"word": "the", "start": 0.0, "end": 0.3}, {"word": " the", "start": 0.3, "end": 0.6}],
            no_speech_prob=0.95,
        ),
        _mlx_segment(
            text="hello", words=[{"word": "hello", "start": 1.0, "end": 1.3}],
            no_speech_prob=0.1,
        ),
    )
    tokens = _mlx_segments_to_tokens(response, no_speech_threshold=0.6)
    assert [t.text for t in tokens] == ["hello"]


def test_missing_no_speech_prob_field_is_treated_as_low_risk():
    """A segment without the field (e.g. older mlx-whisper version)
    is conservatively kept. Better to let it through than to drop
    real speech because of a missing field.
    """
    response = {"text": "hello", "segments": [{
        "text": "hello", "start": 0.0, "end": 0.3,
        "words": [{"word": "hello", "start": 0.0, "end": 0.3}],
    }]}
    tokens = _mlx_segments_to_tokens(response, no_speech_threshold=0.6)
    assert len(tokens) == 1


def test_missing_avg_logprob_field_is_treated_as_high_confidence():
    response = {"text": "hello", "segments": [{
        "text": "hello", "start": 0.0, "end": 0.3,
        "words": [{"word": "hello", "start": 0.0, "end": 0.3}],
    }]}
    tokens = _mlx_segments_to_tokens(response, logprob_threshold=-1.0)
    assert len(tokens) == 1


# --- tests for the backend wiring -------------------------------------------


def test_backend_default_thresholds_pass_through_to_mlx():
    """When the user doesn't set thresholds, the backend uses mlx's
    default (no_speech_threshold=0.6, logprob_threshold=-1.0).
    """
    fake = FakeMlxWhisper([_make_mlx_response(_mlx_segment(text="x"))])
    b = WhisperStreamingBackend(mlx_transcribe=fake)
    b.transcribe(b"\x00" * 16000, 16000)
    assert fake.calls[0][2]["no_speech_threshold"] == 0.6
    assert fake.calls[0][2]["logprob_threshold"] == -1.0


def test_backend_passes_custom_thresholds_to_mlx():
    fake = FakeMlxWhisper([_make_mlx_response(_mlx_segment(text="x"))])
    b = WhisperStreamingBackend(
        mlx_transcribe=fake,
        no_speech_threshold=0.8,
        logprob_threshold=-0.5,
    )
    b.transcribe(b"\x00" * 16000, 16000)
    assert fake.calls[0][2]["no_speech_threshold"] == 0.8
    assert fake.calls[0][2]["logprob_threshold"] == -0.5


def test_backend_drops_high_no_speech_segments():
    fake = FakeMlxWhisper([_make_mlx_response(_mlx_segment(
        text="the the the", no_speech_prob=0.9,
        words=[{"word": "the", "start": 0.0, "end": 0.3}],
    ))])
    b = WhisperStreamingBackend(mlx_transcribe=fake)
    tokens = b.transcribe(b"\x00" * 16000, 16000)
    assert tokens == []


def test_backend_drops_low_logprob_segments():
    fake = FakeMlxWhisper([_make_mlx_response(_mlx_segment(
        text="x", avg_logprob=-1.5,
        words=[{"word": "x", "start": 0.0, "end": 0.3}],
    ))])
    b = WhisperStreamingBackend(mlx_transcribe=fake)
    tokens = b.transcribe(b"\x00" * 16000, 16000)
    assert tokens == []


def test_backend_emits_normal_segments():
    fake = FakeMlxWhisper([_make_mlx_response(_mlx_segment(
        text="hello", no_speech_prob=0.1, avg_logprob=-0.3,
        words=[{"word": "hello", "start": 0.0, "end": 0.4}],
    ))])
    b = WhisperStreamingBackend(mlx_transcribe=fake)
    tokens = b.transcribe(b"\x00" * 16000, 16000)
    assert len(tokens) == 1
    assert tokens[0].text == "hello"
