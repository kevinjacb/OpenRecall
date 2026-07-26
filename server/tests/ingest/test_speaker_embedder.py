"""Tests for the SpeakerEmbedder seam — Protocol shape + fake + lazy import.

Mirrors test_whisper_streaming_filters.py: the fake is deterministic and returns
None on a too-short window; importing the module never pulls in numpy/mlx.
"""
from __future__ import annotations

import sys


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


def test_mlx_embedder_imports_lazily_and_not_at_module_import():
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