"""Server policy config — env-driven, validated at startup.

The :class:`AgentConfig` and :class:`GuardrailsConfig` are the single
seam for "what numbers does the agent use?" — every consumer (the
:class:`Planner`, :class:`Guardrails`, the audit logger) reads them
from this module rather than hardcoded literals.

Loading is one-shot at startup via :func:`load_agent_config`; the
returned object is frozen, so once the gateway is running, no caller
can mutate the policy mid-process. A regression in this module is a
test failure: the E2E test pins the default values from the spec.
"""
from __future__ import annotations

from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


# --- canonical default values (per the spec) --------------------------------
DEFAULT_CONFIDENCE_AUTONOMOUS = 0.85
DEFAULT_CONFIDENCE_CONFIRM = 0.60
DEFAULT_RATE_LIMIT_PER_MIN = 20

# Env-var names — pinned as constants so an operator can grep the codebase
# and find every consumer.
ENV_CONFIDENCE_AUTONOMOUS = "OPENRECALL_CONFIDENCE_AUTONOMOUS"
ENV_CONFIDENCE_CONFIRM = "OPENRECALL_CONFIDENCE_CONFIRM"
ENV_RATE_LIMIT_PER_MIN = "OPENRECALL_RATE_LIMIT_PER_MIN"
ENV_WHISPER_NO_SPEECH_THRESHOLD = "OPENRECALL_WHISPER_NO_SPEECH_THRESHOLD"
ENV_WHISPER_LOGPROB_THRESHOLD = "OPENRECALL_WHISPER_LOGPROB_THRESHOLD"


class GuardrailsConfig(BaseModel):
    """The three policy numbers the agent's :class:`Guardrails` consume."""

    # NOTE: bounds are checked in ``_validate_bounds`` so the error
    # message names the env-var the operator set, not pydantic's
    # generic "Input should be less than or equal to 1.0".
    model_config = ConfigDict(frozen=True)
    confidence_autonomous: float = DEFAULT_CONFIDENCE_AUTONOMOUS
    confidence_confirm: float = DEFAULT_CONFIDENCE_CONFIRM
    rate_limit_per_min: int = DEFAULT_RATE_LIMIT_PER_MIN

    @model_validator(mode="after")
    def _validate_bounds(self) -> "GuardrailsConfig":
        if not (0.0 <= self.confidence_autonomous <= 1.0):
            raise ValueError(
                f"{ENV_CONFIDENCE_AUTONOMOUS}={self.confidence_autonomous} "
                f"must be in [0.0, 1.0]"
            )
        if not (0.0 <= self.confidence_confirm <= 1.0):
            raise ValueError(
                f"{ENV_CONFIDENCE_CONFIRM}={self.confidence_confirm} "
                f"must be in [0.0, 1.0]"
            )
        if self.confidence_autonomous <= self.confidence_confirm:
            raise ValueError(
                f"{ENV_CONFIDENCE_AUTONOMOUS} ({self.confidence_autonomous}) must be > "
                f"{ENV_CONFIDENCE_CONFIRM} ({self.confidence_confirm}); otherwise the gate is meaningless."
            )
        if self.rate_limit_per_min <= 0:
            raise ValueError(
                f"{ENV_RATE_LIMIT_PER_MIN}={self.rate_limit_per_min} must be > 0"
            )
        return self


class WhisperConfig(BaseModel):
    """Server-side noise filtering for the streaming transcriber.

    These thresholds drop hallucinated transcripts on quiet inputs
    (low-SNR rooms, low-quality mics). They're the second line of
    defense after the firmware's VAD — and the binding one for
    the XIAO onboard-mic era until the INMP144s arrive.

    A segment is dropped if either
    ``no_speech_prob > no_speech_threshold`` (mlx thinks the audio
    is silence) or ``avg_logprob < logprob_threshold`` (mlx is
    uncertain about what it heard). Both defaults match mlx-whisper's
    built-in defaults.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    no_speech_threshold: float = 0.6
    logprob_threshold: float = -1.0

    @model_validator(mode="after")
    def _validate_bounds(self) -> "WhisperConfig":
        if not (0.0 <= self.no_speech_threshold <= 1.0):
            raise ValueError(
                f"{ENV_WHISPER_NO_SPEECH_THRESHOLD}={self.no_speech_threshold} "
                f"must be in [0.0, 1.0]"
            )
        if self.logprob_threshold > 0.0:
            # avg_logprob is bounded above by 0; the threshold is a
            # "minimum acceptable" so it must be <= 0.
            raise ValueError(
                f"{ENV_WHISPER_LOGPROB_THRESHOLD}={self.logprob_threshold} "
                f"must be <= 0.0"
            )
        return self


class AgentConfig(BaseModel):
    """The full server config — guardrails + whisper noise filtering.
    Future policy (extraction interval, model override, etc.) lives here."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    whisper: WhisperConfig = Field(default_factory=WhisperConfig)


# --- env loader --------------------------------------------------------------


def _parse_float(name: str, raw: str) -> float:
    try:
        return float(raw)
    except ValueError as e:
        raise ValueError(f"{name}={raw!r} is not a valid float") from e


def _parse_int(name: str, raw: str) -> int:
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"{name}={raw!r} is not a valid integer") from e


def load_agent_config(env: Mapping[str, str]) -> AgentConfig:
    """Build the :class:`AgentConfig` from a process environment.

    Call once at startup; pass the result to the constructor of every
    configurable consumer. A bad value raises with a clear message; the
    gateway should refuse to start rather than run with the wrong
    policy.
    """
    guardrails_kwargs: dict = {}
    if ENV_CONFIDENCE_AUTONOMOUS in env:
        guardrails_kwargs["confidence_autonomous"] = _parse_float(
            ENV_CONFIDENCE_AUTONOMOUS, env[ENV_CONFIDENCE_AUTONOMOUS]
        )
    if ENV_CONFIDENCE_CONFIRM in env:
        guardrails_kwargs["confidence_confirm"] = _parse_float(
            ENV_CONFIDENCE_CONFIRM, env[ENV_CONFIDENCE_CONFIRM]
        )
    if ENV_RATE_LIMIT_PER_MIN in env:
        guardrails_kwargs["rate_limit_per_min"] = _parse_int(
            ENV_RATE_LIMIT_PER_MIN, env[ENV_RATE_LIMIT_PER_MIN]
        )
    whisper_kwargs: dict = {}
    if ENV_WHISPER_NO_SPEECH_THRESHOLD in env:
        whisper_kwargs["no_speech_threshold"] = _parse_float(
            ENV_WHISPER_NO_SPEECH_THRESHOLD, env[ENV_WHISPER_NO_SPEECH_THRESHOLD]
        )
    if ENV_WHISPER_LOGPROB_THRESHOLD in env:
        whisper_kwargs["logprob_threshold"] = _parse_float(
            ENV_WHISPER_LOGPROB_THRESHOLD, env[ENV_WHISPER_LOGPROB_THRESHOLD]
        )
    return AgentConfig(
        guardrails=GuardrailsConfig(**guardrails_kwargs),
        whisper=WhisperConfig(**whisper_kwargs),
    )
