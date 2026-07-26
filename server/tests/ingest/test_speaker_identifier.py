"""Tests for the SpeakerIdentifier — recognition, clustering, enrollment.

A fake embedder returns a fixed vector so the registry's seeded centroid
determines the cosine match. Recognition is the focus here: confirmed/tentative/
fall-through, no-poison-on-single-tentative, promotion-after-N, embed-failure.
"""
from __future__ import annotations

import math

import pytest

from sense_server.ingest.speaker_config import SpeakerConfig
from sense_server.ingest.speaker_embedder import FakeSpeakerEmbedder
from sense_server.memory.speaker_registry import InMemorySpeakerRegistry, Speaker
from sense_server.ingest.speaker_identifier import (
    SpeakerAssignment,
    SpeakerIdentifier,
)


def _vec_cos_e0(dim, t):
    """A unit vector whose cosine with e0=[1,0,...] is exactly ``t``.

    First component ``t``, second ``sqrt(1-t^2)``, rest 0. Already unit length,
    so cosine(_vec_cos_e0(d,t), e0) == t for any t in [-1, 1].
    """
    v = [0.0] * dim
    v[0] = t
    v[1] = math.sqrt(max(0.0, 1.0 - t * t))
    return v


def e0(dim):
    """The first basis vector — cosine with itself is 1.0."""
    return _vec_cos_e0(dim, 1.0)


def _pcm(ms: int = 1000, sr: int = 16000) -> bytes:
    return bytes((i % 256) for i in range(sr * ms // 1000 * 2))


def _reg_with_speaker(sid, centroid, dim=8):
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    reg.add_speaker(Speaker(
        speaker_id=sid, display_name=None, is_wearer=False,
        enrollment_status="confirmed", centroid=centroid, embedding_model="fake",
        dim=dim, turn_count=5, first_seen="2026-07-26T00:00:00+00:00",
        updated_at="2026-07-26T00:00:00+00:00",
    ))
    return reg


class _EmbedReturns:
    """Test embedder that always returns a fixed vector (ignores PCM)."""

    def __init__(self, v):
        self.v = list(v)
        self.dim = len(self.v)

    def embed(self, pcm, sr):
        return list(self.v)


# --- recognition ------------------------------------------------------------


def test_identify_returns_none_for_silence_window():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    ident = SpeakerIdentifier(
        FakeSpeakerEmbedder(dim=8, min_speech_ms=500), reg, SpeakerConfig(),
    )
    assert ident.identify(b"\x00" * 100, 16000) is None  # too short -> None


def test_identify_confirmed_match_when_cosine_above_confirm_threshold():
    c = e0(8)
    reg = _reg_with_speaker("s1", c)
    ident = SpeakerIdentifier(_EmbedReturns(c), reg, SpeakerConfig())
    a = ident.identify(_pcm(), 16000)
    assert a is not None and a.speaker_id == "s1"
    assert a.assignment == "confirmed"
    assert a.confidence >= 0.7


def test_identify_tentative_match_does_not_touch_centroid():
    c = e0(8)
    reg = _reg_with_speaker("s1", c)
    near = _vec_cos_e0(8, 0.6)  # cosine 0.6 -> tentative band [0.55, 0.7)
    ident = SpeakerIdentifier(_EmbedReturns(near), reg, SpeakerConfig())
    a = ident.identify(_pcm(), 16000)
    assert a is not None and a.assignment == "tentative"
    # ring buffer untouched (no confirmed embedding added)
    assert reg.ring_buffer("s1") == []
    assert reg.centroid("s1") == c  # unchanged


def test_identify_promotes_tentative_to_confirmed_after_n_agreeing():
    c = e0(8)
    reg = _reg_with_speaker("s1", c)
    near = _vec_cos_e0(8, 0.6)
    ident = SpeakerIdentifier(_EmbedReturns(near), reg, SpeakerConfig(
        corroborate_n=3, confirm_threshold=0.7, tentative_threshold=0.55))
    outs = [ident.identify(_pcm(), 16000) for _ in range(3)]
    assert outs[-1].assignment == "confirmed"
    assert reg.ring_buffer("s1")  # promoted -> folded into ring buffer


def test_identify_falling_below_tentative_does_not_assign():
    c = e0(8)
    reg = _reg_with_speaker("s1", c)
    far = _vec_cos_e0(8, 0.3)  # cosine 0.3 < 0.55 -> below tentative
    ident = SpeakerIdentifier(_EmbedReturns(far), reg, SpeakerConfig())
    assert ident.identify(_pcm(), 16000) is None  # fall-through to clustering


def test_identify_returns_none_when_embedder_raises():
    c = e0(8)
    reg = _reg_with_speaker("s1", c)

    class _Boom:
        dim = 8

        def embed(self, pcm, sr):
            raise RuntimeError("model oom")

    ident = SpeakerIdentifier(_Boom(), reg, SpeakerConfig())
    assert ident.identify(_pcm(), 16000) is None  # transcription unblocked