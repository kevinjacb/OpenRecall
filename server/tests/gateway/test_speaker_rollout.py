"""Rollout guard for speaker recognition.

When OPENRECALL_SPEAKER_ENABLED=false the gateway must wire NO SpeakerIdentifier
into the pipeline — zero embed calls and speaker=None on every Transcript.
``build_speaker_identifier`` centralizes that decision so the rollout guard is
structural, not a runtime branch the hot path has to check.
"""
from __future__ import annotations

from openrecall_server.gateway.adapter import build_pipeline_factory, build_speaker_identifier
from openrecall_server.ingest.speaker_config import SpeakerConfig
from openrecall_server.memory.speaker_registry import InMemorySpeakerRegistry


def test_disabled_config_yields_no_identifier():
    cfg = SpeakerConfig(enabled=False)
    ident = build_speaker_identifier(cfg, InMemorySpeakerRegistry(cfg), embedder=None)
    assert ident is None


def test_enabled_config_yields_identifier():
    cfg = SpeakerConfig(enabled=True, embed_model="fake")
    ident = build_speaker_identifier(cfg, InMemorySpeakerRegistry(cfg), embedder="fake")
    assert ident is not None


def test_build_pipeline_factory_accepts_speaker_identifier():
    # The factory must accept an optional speaker_identifier and not require
    # heavy deps to construct (it stays lazy). We don't call the returned
    # factory (that needs opuslib); we only assert the seam exists.
    factory = build_pipeline_factory(use_streaming=False, speaker_identifier=None)
    assert factory is not None


def test_resemblyzer_model_yields_resemblyzer_embedder():
    from openrecall_server.ingest.speaker_embedder import ResemblyzerSpeakerEmbedder

    cfg = SpeakerConfig(enabled=True, embed_model="resemblyzer")
    ident = build_speaker_identifier(cfg, InMemorySpeakerRegistry(cfg), embedder=None)
    assert ident is not None
    # Construction is cheap (no model load) so this assertion is dep-free.
    assert isinstance(ident._embedder, ResemblyzerSpeakerEmbedder)


def test_unset_embed_model_yields_fake_embedder():
    from openrecall_server.ingest.speaker_embedder import FakeSpeakerEmbedder

    cfg = SpeakerConfig(enabled=True)  # embed_model defaults to ""
    ident = build_speaker_identifier(cfg, InMemorySpeakerRegistry(cfg), embedder=None)
    assert ident is not None
    assert isinstance(ident._embedder, FakeSpeakerEmbedder)


def test_explicit_fake_overrides_resemblyzer_model():
    from openrecall_server.ingest.speaker_embedder import FakeSpeakerEmbedder

    cfg = SpeakerConfig(enabled=True, embed_model="resemblyzer")
    # Explicit "fake" sentinel overrides cfg.embed_model.
    ident = build_speaker_identifier(cfg, InMemorySpeakerRegistry(cfg), embedder="fake")
    assert ident is not None
    assert isinstance(ident._embedder, FakeSpeakerEmbedder)