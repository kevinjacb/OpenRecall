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


# --- WhisperConfig anti-hallucination fields ---------------------------------
# Beyond the per-segment no_speech/logprob gates, Whisper has a third failure
# mode on near-silent / low-SNR audio that the firmware VAD admits: it
# autoregressively emits a *short, confident* training phrase ("Thank you.",
# "Hello.", "Thanks for watching.") with good no_speech_prob / avg_logprob, so
# the confidence filter lets it through. The fields below add three new
# defenses, all env-tunable:
#   - a phrase blocklist (drops short segments matching known phantoms),
#   - compression_ratio_threshold + condition_on_previous_text=False (stops
#     hallucinations from looping within a segment and propagating across
#     hops),
#   - an opt-in spectral VAD gate (webrtcvad) that skips the Whisper call
#     entirely on pure-noise hops.


def test_whisper_anti_hallucination_defaults():
    cfg = WhisperConfig()
    assert cfg.compression_ratio_threshold == 2.4
    assert cfg.condition_on_previous_text is False
    assert cfg.hallucination_blocklist_enabled is True
    assert cfg.hallucination_max_words == 4
    assert cfg.hallucination_phrases is None  # None -> built-in default set
    assert cfg.vad_mode is None              # None -> VAD gate disabled
    assert cfg.vad_aggressiveness == 3


def test_whisper_compression_ratio_threshold_from_env():
    cfg = load_agent_config({"OPENRECALL_WHISPER_COMPRESSION_RATIO_THRESHOLD": "3.0"})
    assert cfg.whisper.compression_ratio_threshold == 3.0


def test_whisper_compression_ratio_threshold_invalid_raises():
    with pytest.raises(ValueError, match="OPENRECALL_WHISPER_COMPRESSION_RATIO_THRESHOLD"):
        WhisperConfig(compression_ratio_threshold=0.0)
    with pytest.raises(ValueError, match="OPENRECALL_WHISPER_COMPRESSION_RATIO_THRESHOLD"):
        load_agent_config({"OPENRECALL_WHISPER_COMPRESSION_RATIO_THRESHOLD": "not-a-number"})


def test_whisper_condition_on_previous_text_from_env():
    cfg = load_agent_config({"OPENRECALL_WHISPER_CONDITION_ON_PREVIOUS_TEXT": "true"})
    assert cfg.whisper.condition_on_previous_text is True
    cfg = load_agent_config({"OPENRECALL_WHISPER_CONDITION_ON_PREVIOUS_TEXT": "0"})
    assert cfg.whisper.condition_on_previous_text is False


def test_whisper_condition_on_previous_text_invalid_raises():
    with pytest.raises(ValueError, match="OPENRECALL_WHISPER_CONDITION_ON_PREVIOUS_TEXT"):
        load_agent_config({"OPENRECALL_WHISPER_CONDITION_ON_PREVIOUS_TEXT": "maybe"})


def test_whisper_hallucination_blocklist_enabled_from_env():
    cfg = load_agent_config({"OPENRECALL_WHISPER_HALLUCINATION_BLOCKLIST_ENABLED": "false"})
    assert cfg.whisper.hallucination_blocklist_enabled is False


def test_whisper_hallucination_max_words_from_env():
    cfg = load_agent_config({"OPENRECALL_WHISPER_HALLUCINATION_MAX_WORDS": "6"})
    assert cfg.whisper.hallucination_max_words == 6


def test_whisper_hallucination_max_words_invalid_raises():
    with pytest.raises(ValueError, match="OPENRECALL_WHISPER_HALLUCINATION_MAX_WORDS"):
        WhisperConfig(hallucination_max_words=0)
    with pytest.raises(ValueError, match="OPENRECALL_WHISPER_HALLUCINATION_MAX_WORDS"):
        load_agent_config({"OPENRECALL_WHISPER_HALLUCINATION_MAX_WORDS": "two"})


def test_whisper_hallucination_phrases_from_env():
    """Comma-separated env overrides the built-in default set."""
    cfg = load_agent_config({"OPENRECALL_WHISPER_HALLUCINATION_PHRASES": "thank you, hello, bye"})
    assert cfg.whisper.hallucination_phrases == ("thank you", "hello", "bye")


def test_whisper_hallucination_phrases_empty_env_means_disable_via_empty_tuple():
    """An empty list is a valid (if unusual) "block nothing custom" override;
    the backend still uses the built-in set only when the field is None."""
    cfg = load_agent_config({"OPENRECALL_WHISPER_HALLUCINATION_PHRASES": ""})
    assert cfg.whisper.hallucination_phrases == ()


def test_whisper_vad_mode_from_env():
    cfg = load_agent_config({"OPENRECALL_WHISPER_VAD_MODE": "webrtc"})
    assert cfg.whisper.vad_mode == "webrtc"
    # Empty string disables (treated as None).
    cfg = load_agent_config({"OPENRECALL_WHISPER_VAD_MODE": ""})
    assert cfg.whisper.vad_mode is None


def test_whisper_vad_mode_invalid_raises():
    with pytest.raises(ValueError, match="OPENRECALL_WHISPER_VAD_MODE"):
        load_agent_config({"OPENRECALL_WHISPER_VAD_MODE": "spectral"})


def test_whisper_vad_aggressiveness_from_env():
    cfg = load_agent_config({"OPENRECALL_WHISPER_VAD_AGGRESSIVENESS": "1"})
    assert cfg.whisper.vad_aggressiveness == 1


def test_whisper_vad_aggressiveness_out_of_range_raises():
    for bad in ("-1", "4", "9"):
        with pytest.raises(ValueError, match="OPENRECALL_WHISPER_VAD_AGGRESSIVENESS"):
            WhisperConfig(vad_aggressiveness=int(bad))


# --- ASR backend selection (whisper <-> parakeet switch) ---------------------


def test_asr_backend_defaults_to_whisper():
    """The default must be whisper: adding the Parakeet option changes
    nothing for an operator who sets no new env vars."""
    cfg = load_agent_config({})
    assert cfg.asr.backend == "whisper"
    assert cfg.asr.parakeet_model == "mlx-community/parakeet-tdt-0.6b-v3"


def test_asr_backend_parakeet_from_env():
    cfg = load_agent_config({"OPENRECALL_ASR_BACKEND": "parakeet"})
    assert cfg.asr.backend == "parakeet"


def test_asr_backend_is_case_and_whitespace_insensitive():
    cfg = load_agent_config({"OPENRECALL_ASR_BACKEND": "  Parakeet "})
    assert cfg.asr.backend == "parakeet"


def test_asr_backend_empty_env_falls_back_to_whisper():
    """Blanking the var is a valid way to revert, not a validation error."""
    cfg = load_agent_config({"OPENRECALL_ASR_BACKEND": ""})
    assert cfg.asr.backend == "whisper"


def test_asr_backend_invalid_raises():
    with pytest.raises(ValueError, match="OPENRECALL_ASR_BACKEND"):
        load_agent_config({"OPENRECALL_ASR_BACKEND": "deepgram"})


def test_parakeet_model_override_from_env():
    cfg = load_agent_config({
        "OPENRECALL_ASR_BACKEND": "parakeet",
        "OPENRECALL_PARAKEET_MODEL": "mlx-community/parakeet-tdt-1.1b",
    })
    assert cfg.asr.parakeet_model == "mlx-community/parakeet-tdt-1.1b"


def test_parakeet_model_blank_env_keeps_default():
    cfg = load_agent_config({"OPENRECALL_PARAKEET_MODEL": "   "})
    assert cfg.asr.parakeet_model == "mlx-community/parakeet-tdt-0.6b-v3"


def test_parakeet_model_empty_value_raises_on_direct_construction():
    from openrecall_server.agent.config import AsrConfig

    with pytest.raises(ValueError, match="OPENRECALL_PARAKEET_MODEL"):
        AsrConfig(parakeet_model="  ")


def test_whisper_filters_are_unaffected_by_backend_choice():
    """Selecting parakeet must not silently disturb the whisper config, so
    flipping back is a pure revert."""
    cfg = load_agent_config({"OPENRECALL_ASR_BACKEND": "parakeet"})
    assert cfg.whisper.no_speech_threshold == 0.6
    assert cfg.whisper.hallucination_blocklist_enabled is True
