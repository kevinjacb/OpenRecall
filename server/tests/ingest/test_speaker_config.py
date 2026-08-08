"""Tests for SpeakerConfig — env-driven, validated at startup."""
from __future__ import annotations

import pytest

from opensapien_server.ingest.speaker_config import load_speaker_config


def test_defaults_when_disabled_and_unset():
    cfg = load_speaker_config({})
    assert cfg.enabled is False
    assert cfg.confirm_threshold == 0.7
    assert cfg.tentative_threshold == 0.55
    assert cfg.corroborate_n == 3
    assert cfg.ring_buffer_n == 100
    assert cfg.ema_alpha == 0.05
    assert cfg.name_nudge_turns == 8


def test_env_overrides_are_parsed():
    cfg = load_speaker_config({
        "OPENSAPIEN_SPEAKER_ENABLED": "true",
        "OPENSAPIEN_SPEAKER_CONFIRM_THRESHOLD": "0.8",
        "OPENSAPIEN_SPEAKER_RING_BUFFER_N": "50",
    })
    assert cfg.enabled is True
    assert cfg.confirm_threshold == 0.8
    assert cfg.ring_buffer_n == 50


def test_confirm_must_exceed_tentative():
    with pytest.raises(ValueError, match="confirm_threshold"):
        load_speaker_config({
            "OPENSAPIEN_SPEAKER_ENABLED": "true",
            "OPENSAPIEN_SPEAKER_CONFIRM_THRESHOLD": "0.5",
            "OPENSAPIEN_SPEAKER_TENTATIVE_THRESHOLD": "0.6",
        })


def test_thresholds_must_be_in_unit_interval():
    with pytest.raises(ValueError):
        load_speaker_config({"OPENSAPIEN_SPEAKER_CONFIRM_THRESHOLD": "1.5"})