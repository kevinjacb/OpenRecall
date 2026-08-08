"""Tests for the policy-config module (server config)."""
from __future__ import annotations

import pytest

from openrecall_server.agent.config import (
    AgentConfig,
    GuardrailsConfig,
    WhisperConfig,
    load_agent_config,
)


def test_defaults_when_env_empty():
    cfg = load_agent_config({})
    assert cfg.guardrails.confidence_autonomous == 0.85
    assert cfg.guardrails.confidence_confirm == 0.60
    assert cfg.guardrails.rate_limit_per_min == 20


def test_overrides_from_env():
    env = {
        "OPENRECALL_CONFIDENCE_AUTONOMOUS": "0.9",
        "OPENRECALL_CONFIDENCE_CONFIRM": "0.5",
        "OPENRECALL_RATE_LIMIT_PER_MIN": "100",
    }
    cfg = load_agent_config(env)
    assert cfg.guardrails.confidence_autonomous == 0.9
    assert cfg.guardrails.confidence_confirm == 0.5
    assert cfg.guardrails.rate_limit_per_min == 100


def test_invalid_confidence_value_raises():
    with pytest.raises(ValueError, match="OPENRECALL_CONFIDENCE_AUTONOMOUS"):
        load_agent_config({"OPENRECALL_CONFIDENCE_AUTONOMOUS": "not-a-number"})


def test_out_of_range_confidence_raises():
    with pytest.raises(ValueError, match="OPENRECALL_CONFIDENCE_AUTONOMOUS"):
        load_agent_config({"OPENRECALL_CONFIDENCE_AUTONOMOUS": "1.5"})


def test_negative_rate_limit_raises():
    with pytest.raises(ValueError, match="OPENRECALL_RATE_LIMIT_PER_MIN"):
        load_agent_config({"OPENRECALL_RATE_LIMIT_PER_MIN": "0"})


def test_non_integer_rate_limit_raises():
    with pytest.raises(ValueError, match="OPENRECALL_RATE_LIMIT_PER_MIN"):
        load_agent_config({"OPENRECALL_RATE_LIMIT_PER_MIN": "twenty"})


def test_threshold_ordering_validated():
    """autonomous must be > confirm; otherwise the gate is meaningless."""
    with pytest.raises(ValueError, match="must be >"):
        load_agent_config({
            "OPENRECALL_CONFIDENCE_AUTONOMOUS": "0.5",
            "OPENRECALL_CONFIDENCE_CONFIRM": "0.7",
        })


def test_guardrails_config_defaults_in_range():
    cfg = GuardrailsConfig()
    assert 0.0 < cfg.confidence_confirm < cfg.confidence_autonomous <= 1.0
    assert cfg.rate_limit_per_min > 0


def test_agent_config_is_frozen():
    from pydantic import ValidationError
    cfg = AgentConfig()
    with pytest.raises(ValidationError):
        cfg.guardrails = GuardrailsConfig()  # type: ignore[misc]


# --- WhisperConfig tests -----------------------------------------------------


def test_whisper_defaults_match_mlx():
    cfg = WhisperConfig()
    assert cfg.no_speech_threshold == 0.6
    assert cfg.logprob_threshold == -1.0


def test_whisper_overrides_from_env():
    env = {
        "OPENRECALL_WHISPER_NO_SPEECH_THRESHOLD": "0.8",
        "OPENRECALL_WHISPER_LOGPROB_THRESHOLD": "-0.5",
    }
    cfg = load_agent_config(env)
    assert cfg.whisper.no_speech_threshold == 0.8
    assert cfg.whisper.logprob_threshold == -0.5


def test_whisper_no_speech_out_of_range_raises():
    with pytest.raises(ValueError, match="OPENRECALL_WHISPER_NO_SPEECH_THRESHOLD"):
        WhisperConfig(no_speech_threshold=1.5)
    with pytest.raises(ValueError, match="OPENRECALL_WHISPER_NO_SPEECH_THRESHOLD"):
        WhisperConfig(no_speech_threshold=-0.1)


def test_whisper_logprob_above_zero_raises():
    with pytest.raises(ValueError, match="OPENRECALL_WHISPER_LOGPROB_THRESHOLD"):
        WhisperConfig(logprob_threshold=0.1)


def test_whisper_logprob_zero_is_allowed():
    """logprob_threshold=0 means "drop everything" — an extreme but
    valid setting for paranoid operator setups. Allow it (the bound
    check uses >, not >=)."""
    cfg = WhisperConfig(logprob_threshold=0.0)
    assert cfg.logprob_threshold == 0.0


def test_whisper_logprob_invalid_value_raises_at_loader():
    with pytest.raises(ValueError, match="OPENRECALL_WHISPER_LOGPROB_THRESHOLD"):
        load_agent_config({"OPENRECALL_WHISPER_LOGPROB_THRESHOLD": "not-a-number"})


def test_whisper_logprob_at_threshold_is_kept():
    """The `_validate_bounds` uses strict `>` so logprob_threshold=0
    is allowed; segments at exactly avg_logprob=0 are kept (the
    logprob check is `<`, not `<=`)."""
    # This is a semantic contract test — verify the validator logic.
    cfg = WhisperConfig(logprob_threshold=0.0)
    # We can't directly test the filter logic here (that's in
    # _mlx_segments_to_tokens), but we verify the config accepts 0.0.
    assert cfg.logprob_threshold == 0.0


def test_agent_config_contains_whisper_section_by_default():
    cfg = AgentConfig()
    assert hasattr(cfg, "whisper")
    assert isinstance(cfg.whisper, WhisperConfig)


def test_agent_config_default_load_returns_whisper_defaults():
    cfg = load_agent_config({})
    assert cfg.whisper.no_speech_threshold == 0.6
    assert cfg.whisper.logprob_threshold == -1.0
