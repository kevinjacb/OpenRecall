"""Rollout guard for speaker recognition.

When SENSE_SPEAKER_ENABLED=false the gateway must wire NO SpeakerIdentifier
into the pipeline — zero embed calls and speaker=None on every Transcript.
``build_speaker_identifier`` centralizes that decision so the rollout guard is
structural, not a runtime branch the hot path has to check.
"""
from __future__ import annotations

from sense_server.gateway.adapter import build_pipeline_factory, build_speaker_identifier
from sense_server.ingest.speaker_config import SpeakerConfig
from sense_server.memory.speaker_registry import InMemorySpeakerRegistry


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