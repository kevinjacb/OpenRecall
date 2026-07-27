"""Real-backend integration test for ResemblyzerSpeakerEmbedder.

Skipif resemblyzer is not installed (the `speaker` extra) OR the fixture wavs
are missing. Asserts the real embedder separates two speakers: same-speaker
cosine (two utterances of speaker A) > diff-speaker cosine (A vs B) by a
margin. This is the only automated proof that the real recognition path
actually discriminates voices; the fake embedder's distance is synthetic.
"""
from __future__ import annotations

import importlib.util
import os
import wave

import pytest

_FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures", "audio")
_has_resemblyzer = importlib.util.find_spec("resemblyzer") is not None
_has_fixtures = all(
    os.path.exists(os.path.join(_FIXTURES, n))
    for n in ("speaker_a_1.wav", "speaker_a_2.wav", "speaker_b_1.wav")
)


def _load_pcm(path: str) -> bytes:
    with wave.open(path, "rb") as r:
        assert r.getframerate() == 16000, f"expected 16 kHz, got {r.getframerate()}"
        return r.readframes(r.getnframes())


@pytest.mark.skipif(
    not _has_resemblyzer or not _has_fixtures,
    reason="resemblyzer extra or speaker fixtures not present",
)
def test_resemblyzer_separates_two_speakers():
    from sense_server.ingest.speaker_embedder import ResemblyzerSpeakerEmbedder
    from sense_server.ingest.speaker_identifier import _cosine

    emb = ResemblyzerSpeakerEmbedder(min_speech_ms=500)
    emb.warmup()
    a1 = emb.embed(_load_pcm(os.path.join(_FIXTURES, "speaker_a_1.wav")), 16000)
    a2 = emb.embed(_load_pcm(os.path.join(_FIXTURES, "speaker_a_2.wav")), 16000)
    b1 = emb.embed(_load_pcm(os.path.join(_FIXTURES, "speaker_b_1.wav")), 16000)
    assert a1 is not None and a2 is not None and b1 is not None, "embed returned None"
    assert len(a1) == len(a2) == len(b1), "dim mismatch across clips"

    sim_same = _cosine(a1, a2)
    sim_diff = _cosine(a1, b1)
    # Resemblyzer same-speaker cosine typically 0.7-0.9, diff 0.2-0.4. Require a
    # clear margin. Tune the constant down only if a specific LibriSpeech pair
    # is unusually close; 0.15 is conservative.
    margin = 0.15
    assert sim_same - sim_diff > margin, (
        f"same-speaker {sim_same:.3f} not clearly > diff-speaker {sim_diff:.3f} "
        f"(margin {margin})"
    )


@pytest.mark.skipif(
    not _has_resemblyzer or not _has_fixtures,
    reason="resemblyzer extra or speaker fixtures not present",
)
def test_resemblyzer_dim_matches_across_clips():
    # dim is discovered from the model and must be consistent across hops.
    from sense_server.ingest.speaker_embedder import ResemblyzerSpeakerEmbedder

    emb = ResemblyzerSpeakerEmbedder()
    emb.warmup()
    d = emb.dim
    v = emb.embed(_load_pcm(os.path.join(_FIXTURES, "speaker_a_1.wav")), 16000)
    assert v is not None and len(v) == d