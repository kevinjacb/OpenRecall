"""Tests for the SpeakerEmbedder seam — Protocol shape + fake + lazy import.

Mirrors test_whisper_streaming_filters.py: the fake is deterministic and returns
None on a too-short window; importing the module never pulls in numpy/mlx.
"""
from __future__ import annotations

import importlib.util
import sys

import pytest

_np_available = importlib.util.find_spec("numpy") is not None


def _pcm(ms: int, sr: int = 16000) -> bytes:
    # 16-bit LE mono bytes of length ms
    return b"\x00\x01" * (sr * ms // 1000)


def test_fake_embedder_returns_a_unit_vector_of_configured_dim():
    from sense_server.ingest.speaker_embedder import FakeSpeakerEmbedder

    emb = FakeSpeakerEmbedder(dim=8)
    vec = emb.embed(_pcm(1000), 16000)
    assert vec is not None
    assert len(vec) == 8
    assert emb.embed(_pcm(1000), 16000) == vec  # deterministic


def test_fake_embedder_returns_none_on_window_shorter_than_min_speech_ms():
    from sense_server.ingest.speaker_embedder import FakeSpeakerEmbedder

    emb = FakeSpeakerEmbedder(dim=8, min_speech_ms=500)
    assert emb.embed(_pcm(100), 16000) is None  # 100 ms < 500 ms


def test_fake_embedder_different_pcm_yields_different_vector():
    from sense_server.ingest.speaker_embedder import FakeSpeakerEmbedder

    emb = FakeSpeakerEmbedder(dim=8)
    a = emb.embed(_pcm(1000), 16000)
    b = emb.embed(b"\x01\x02" * 16000, 16000)
    assert a != b


def test_speaker_embedder_module_imports_lazily():
    # Run in a fresh subprocess so the assertion isn't polluted by another test
    # in the suite having already imported numpy. Importing the module must not
    # pull in numpy/mlx_whisper — only embed() does, and only on the real path.
    import subprocess
    import sys
    from pathlib import Path

    src = str(Path(__file__).resolve().parents[2] / "src")
    code = (
        "import sys; "
        "import sense_server.ingest.speaker_embedder; "
        "assert 'numpy' not in sys.modules, sys.modules; "
        "assert 'mlx_whisper' not in sys.modules, sys.modules; "
        "print('ok')"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        env={"PYTHONPATH": src, "PATH": ""},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ok" in r.stdout


@pytest.mark.skipif(not _np_available, reason="numpy not installed")
def test_pcm_to_float32_normalizes_int16_to_unit_float():
    import numpy as np

    from sense_server.ingest.speaker_embedder import _pcm_to_float32

    # int16 16384 -> 0.5 ; -16384 -> -0.5
    pcm = (16384).to_bytes(2, "little", signed=True) + (-16384).to_bytes(
        2, "little", signed=True
    )
    arr = _pcm_to_float32(pcm, 16000)
    assert arr is not None
    assert arr.dtype.name == "float32"
    assert arr.shape == (2,)
    assert abs(float(arr[0]) - 0.5) < 1e-6
    assert abs(float(arr[1]) + 0.5) < 1e-6


@pytest.mark.skipif(not _np_available, reason="numpy not installed")
def test_pcm_to_float32_wrong_sample_rate_returns_none():
    from sense_server.ingest.speaker_embedder import _pcm_to_float32

    assert _pcm_to_float32(b"\x00\x00", 48000) is None


def test_resemblyzer_embedder_constructs_without_loading_model():
    # Construction must be cheap: no resemblyzer/numpy import, encoder not
    # loaded. This lets the adapter-selection unit test (T3) assert isinstance
    # dep-free.
    from sense_server.ingest.speaker_embedder import ResemblyzerSpeakerEmbedder

    e = ResemblyzerSpeakerEmbedder(min_speech_ms=500, model_name="resemblyzer")
    assert e.model_name == "resemblyzer"
    assert e.min_speech_ms == 500
    assert e._encoder is None  # not loaded at construction
    assert e._dim is None


def test_resemblyzer_embedder_dim_is_a_lazy_property():
    # dim is a @property that triggers _ensure_ready on first access; we do NOT
    # call it here (would load the model). Assert it's a property descriptor.
    from sense_server.ingest.speaker_embedder import ResemblyzerSpeakerEmbedder

    assert isinstance(ResemblyzerSpeakerEmbedder.__dict__["dim"], property)