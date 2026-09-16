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


# --- faster-whisper: the portable (CPU/CUDA) backend -------------------------


def test_asr_backend_faster_whisper_is_allowed():
    """The allow-list must admit the portable backend, or every non-Apple
    deployment fails at config load."""
    cfg = load_agent_config({"OPENRECALL_ASR_BACKEND": "faster_whisper"})
    assert cfg.asr.backend == "faster_whisper"


def test_faster_whisper_defaults():
    cfg = load_agent_config({})
    assert cfg.asr.faster_whisper_model == "large-v3-turbo"
    assert cfg.asr.faster_whisper_device == "auto"
    assert cfg.asr.faster_whisper_compute_type == "default"


def test_faster_whisper_model_device_and_compute_type_from_env():
    cfg = load_agent_config({
        "OPENRECALL_ASR_BACKEND": "faster_whisper",
        "OPENRECALL_FASTER_WHISPER_MODEL": "distil-large-v3.5",
        "OPENRECALL_FASTER_WHISPER_DEVICE": " CUDA ",
        "OPENRECALL_FASTER_WHISPER_COMPUTE_TYPE": "FLOAT16",
    })
    assert cfg.asr.faster_whisper_model == "distil-large-v3.5"
    # device/compute type are case-insensitive; the model id is NOT lowercased
    # (HF repo ids are case-sensitive).
    assert cfg.asr.faster_whisper_device == "cuda"
    assert cfg.asr.faster_whisper_compute_type == "float16"


def test_faster_whisper_model_id_case_is_preserved():
    cfg = load_agent_config({
        "OPENRECALL_FASTER_WHISPER_MODEL": "Systran/faster-Whisper-Large-v3",
    })
    assert cfg.asr.faster_whisper_model == "Systran/faster-Whisper-Large-v3"


def test_faster_whisper_blank_env_keeps_defaults():
    cfg = load_agent_config({
        "OPENRECALL_FASTER_WHISPER_MODEL": "  ",
        "OPENRECALL_FASTER_WHISPER_DEVICE": "",
        "OPENRECALL_FASTER_WHISPER_COMPUTE_TYPE": " ",
    })
    assert cfg.asr.faster_whisper_model == "large-v3-turbo"
    assert cfg.asr.faster_whisper_device == "auto"
    assert cfg.asr.faster_whisper_compute_type == "default"


def test_faster_whisper_unknown_device_raises():
    """A typo'd device would otherwise surface as a CTranslate2 error on the
    first audio packet, in a worker thread, in production."""
    with pytest.raises(ValueError, match="OPENRECALL_FASTER_WHISPER_DEVICE"):
        load_agent_config({"OPENRECALL_FASTER_WHISPER_DEVICE": "mps"})


def test_faster_whisper_empty_values_raise_on_direct_construction():
    from openrecall_server.agent.config import AsrConfig

    with pytest.raises(ValueError, match="OPENRECALL_FASTER_WHISPER_MODEL"):
        AsrConfig(faster_whisper_model="  ")
    with pytest.raises(ValueError, match="OPENRECALL_FASTER_WHISPER_COMPUTE_TYPE"):
        AsrConfig(faster_whisper_compute_type=" ")


def test_faster_whisper_uses_hop_scheduling_not_utterance():
    """It is Whisper: its word timestamps are stable across overlapping calls,
    so it takes the rolling-window hop path, not Parakeet's utterance mode."""
    cfg = load_agent_config({"OPENRECALL_ASR_BACKEND": "faster_whisper"})
    assert cfg.asr.resolved_mode() == "hop"


def test_whisper_filters_apply_to_faster_whisper():
    """Same decoder as mlx-whisper, so the Whisper noise filters stay in force
    (unlike Parakeet, where they have no analogue)."""
    cfg = load_agent_config({
        "OPENRECALL_ASR_BACKEND": "faster_whisper",
        "OPENRECALL_WHISPER_NO_SPEECH_THRESHOLD": "0.42",
    })
    assert cfg.whisper.no_speech_threshold == 0.42


# --- CommandDetectorConfig (speech -> command channel) -----------------------

from openrecall_server.agent.config import (
    CommandDetectorConfig,
    DEFAULT_COMMAND_PHRASES,
)


def test_command_config_defaults_off_and_wearer_gated():
    cfg = load_agent_config({})
    assert cfg.command.enabled is False
    assert cfg.command.require_wearer is True
    assert cfg.command.confidence_threshold == 0.8
    assert cfg.command.cooldown_s == 3.0
    assert cfg.command.max_inflight == 1
    assert cfg.command.llm_model is None
    # The day-one photo/video/audio phrases ship by default.
    assert DEFAULT_COMMAND_PHRASES["take a photo"] == "capture_photo"
    assert DEFAULT_COMMAND_PHRASES["start a video"] == "start_video"
    assert "stop video" in DEFAULT_COMMAND_PHRASES
    # The default map is loaded when no env override is given.
    assert cfg.command.phrases == DEFAULT_COMMAND_PHRASES


def test_command_config_env_overrides():
    cfg = load_agent_config({
        "OPENRECALL_COMMAND_DETECTOR_ENABLED": "true",
        "OPENRECALL_COMMAND_REQUIRE_WEARER": "false",
        "OPENRECALL_COMMAND_CONFIDENCE_THRESHOLD": "0.66",
        "OPENRECALL_COMMAND_COOLDOWN_S": "1.5",
        "OPENRECALL_COMMAND_MAX_INFLIGHT": "2",
        "OPENRECALL_COMMAND_LLM_MODEL": "qwen2.5:3b",
        "OPENRECALL_COMMAND_LLM_BASE_URL": "http://x:8000/v1",
        "OPENRECALL_COMMAND_LLM_API_KEY": "sk-x",
        "OPENRECALL_COMMAND_PHRASES": "capture_photo:take a photo;start_video:roll video",
    })
    assert cfg.command.enabled is True
    assert cfg.command.require_wearer is False
    assert cfg.command.confidence_threshold == 0.66
    assert cfg.command.cooldown_s == 1.5
    assert cfg.command.max_inflight == 2
    assert cfg.command.llm_model == "qwen2.5:3b"
    assert cfg.command.llm_base_url == "http://x:8000/v1"
    assert cfg.command.llm_api_key == "sk-x"
    # The env var fully replaces the default map (explicit override).
    assert cfg.command.phrases == {"take a photo": "capture_photo", "roll video": "start_video"}


def test_command_config_empty_phrases_is_deliberate_override():
    cfg = load_agent_config({"OPENRECALL_COMMAND_PHRASES": ""})
    assert cfg.command.phrases == {}


def test_command_config_bad_values_raise():
    import pytest
    with pytest.raises(ValueError, match="OPENRECALL_COMMAND_CONFIDENCE_THRESHOLD"):
        load_agent_config({"OPENRECALL_COMMAND_CONFIDENCE_THRESHOLD": "nope"})
    with pytest.raises(ValueError, match="OPENRECALL_COMMAND_MAX_INFLIGHT"):
        load_agent_config({"OPENRECALL_COMMAND_MAX_INFLIGHT": "x"})


def test_agent_backend_defaults_to_planner():
    cfg = load_agent_config({})
    assert cfg.backend.backend == "planner"
    assert cfg.backend.shadow is False


def test_agent_backend_reads_env():
    cfg = load_agent_config({"OPENRECALL_AGENT_BACKEND": "hermes_with_fallback"})
    assert cfg.backend.backend == "hermes_with_fallback"


def test_agent_backend_rejects_an_unknown_value():
    import pytest
    with pytest.raises(ValueError):
        load_agent_config({"OPENRECALL_AGENT_BACKEND": "magic"})


def test_hermes_timeouts_and_strict_provenance_read_env():
    cfg = load_agent_config({
        "OPENRECALL_HERMES_TIMEOUT_S": "12.5",
        "OPENRECALL_HERMES_PROACTIVE_TIMEOUT_S": "60",
        "OPENRECALL_HERMES_STRICT_PROVENANCE": "true",
    })
    assert cfg.hermes.timeout_s == 12.5
    assert cfg.hermes.proactive_timeout_s == 60.0
    assert cfg.hermes.strict_provenance is True


def test_hermes_defaults():
    cfg = load_agent_config({})
    assert cfg.hermes.timeout_s == 45.0
    assert cfg.hermes.proactive_timeout_s == 90.0
    assert cfg.hermes.strict_provenance is False


def test_agent_shadow_reads_env_true():
    cfg = load_agent_config({"OPENRECALL_AGENT_SHADOW": "true"})
    assert cfg.backend.shadow is True


def test_hermes_timeouts_must_be_positive():
    import pytest
    with pytest.raises(ValueError):
        load_agent_config({"OPENRECALL_HERMES_TIMEOUT_S": "0"})
    with pytest.raises(ValueError):
        load_agent_config({"OPENRECALL_HERMES_PROACTIVE_TIMEOUT_S": "-1"})


# --- P1: where inference runs ------------------------------------------------


def test_inference_url_defaults_to_none():
    """Unset is the rollback: ASR + speaker embedding stay in-process."""
    assert load_agent_config({}).inference.url is None


def test_inference_url_reads_env():
    cfg = load_agent_config({"OPENRECALL_INFERENCE_URL": "http://box:8767"})
    assert cfg.inference.url == "http://box:8767"


def test_inference_timeout_defaults_and_reads_env():
    assert load_agent_config({}).inference.timeout_s == 30.0
    cfg = load_agent_config({"OPENRECALL_INFERENCE_TIMEOUT_S": "12.5"})
    assert cfg.inference.timeout_s == 12.5


def test_inference_timeout_must_be_positive():
    with pytest.raises(ValueError):
        load_agent_config({"OPENRECALL_INFERENCE_TIMEOUT_S": "0"})


def test_blank_inference_url_means_unset():
    """An operator reverts by blanking the var as well as by removing it —
    the same idiom the ASR backend switch uses. A blank string would
    otherwise be a truthy-looking url that every request fails against."""
    cfg = load_agent_config({"OPENRECALL_INFERENCE_URL": "   "})
    assert cfg.inference.url is None


def test_scheduling_family_pairs_whisper_variants_together():
    """What a cross-process mismatch actually breaks is the CADENCE, and every
    Whisper variant shares one. Comparing names instead would flag a `whisper`
    gateway driving a FasterWhisperStreamingBackend — same decoder, same 5s/1s
    cadence, entirely correct — and train the operator to ignore the warning."""
    from openrecall_server.agent.config import scheduling_family

    for name in ("whisper", "faster_whisper", "WhisperStreamingBackend",
                 "FasterWhisperStreamingBackend"):
        assert scheduling_family(name) == "whisper", name


def test_scheduling_family_separates_parakeet():
    """Parakeet is the one that genuinely differs: 2000ms/240ms and utterance
    mode against Whisper's 5000ms/1000ms and hop mode."""
    from openrecall_server.agent.config import scheduling_family

    for name in ("parakeet", "ParakeetStreamingBackend"):
        assert scheduling_family(name) == "parakeet", name
    assert scheduling_family("parakeet") != scheduling_family("whisper")
    assert scheduling_family("parakeet") != scheduling_family("faster_whisper")


def test_scheduling_family_matches_the_window_hop_rule_it_stands_for():
    """The family split must agree with the actual cadence decision in
    run_gateway.py, or this check drifts from what it claims to verify."""
    from openrecall_server.agent.config import scheduling_family

    src = open("scripts/run_gateway.py").read()
    # Both auto-tuning lines branch on parakeet specifically.
    assert 'if agent_config.asr.backend == "parakeet"' in src or \
           '== "parakeet" else' in src, (
        "run_gateway no longer splits window/hop on parakeet; scheduling_family "
        "encodes that split and must be updated with it")
    assert scheduling_family("parakeet") == "parakeet"
