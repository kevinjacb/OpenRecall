"""Tests for the policy-config module (server config)."""
from __future__ import annotations

import pytest

from sense_server.agent.config import (
    AgentConfig,
    GuardrailsConfig,
    load_agent_config,
)


def test_defaults_when_env_empty():
    cfg = load_agent_config({})
    assert cfg.guardrails.confidence_autonomous == 0.85
    assert cfg.guardrails.confidence_confirm == 0.60
    assert cfg.guardrails.rate_limit_per_min == 20


def test_overrides_from_env():
    env = {
        "SENSE_CONFIDENCE_AUTONOMOUS": "0.9",
        "SENSE_CONFIDENCE_CONFIRM": "0.5",
        "SENSE_RATE_LIMIT_PER_MIN": "100",
    }
    cfg = load_agent_config(env)
    assert cfg.guardrails.confidence_autonomous == 0.9
    assert cfg.guardrails.confidence_confirm == 0.5
    assert cfg.guardrails.rate_limit_per_min == 100


def test_invalid_confidence_value_raises():
    with pytest.raises(ValueError, match="SENSE_CONFIDENCE_AUTONOMOUS"):
        load_agent_config({"SENSE_CONFIDENCE_AUTONOMOUS": "not-a-number"})


def test_out_of_range_confidence_raises():
    with pytest.raises(ValueError, match="SENSE_CONFIDENCE_AUTONOMOUS"):
        load_agent_config({"SENSE_CONFIDENCE_AUTONOMOUS": "1.5"})


def test_negative_rate_limit_raises():
    with pytest.raises(ValueError, match="SENSE_RATE_LIMIT_PER_MIN"):
        load_agent_config({"SENSE_RATE_LIMIT_PER_MIN": "0"})


def test_non_integer_rate_limit_raises():
    with pytest.raises(ValueError, match="SENSE_RATE_LIMIT_PER_MIN"):
        load_agent_config({"SENSE_RATE_LIMIT_PER_MIN": "twenty"})


def test_threshold_ordering_validated():
    """autonomous must be > confirm; otherwise the gate is meaningless."""
    with pytest.raises(ValueError, match="must be >"):
        load_agent_config({
            "SENSE_CONFIDENCE_AUTONOMOUS": "0.5",
            "SENSE_CONFIDENCE_CONFIRM": "0.7",
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
