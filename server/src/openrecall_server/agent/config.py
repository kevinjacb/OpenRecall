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
ENV_WHISPER_COMPRESSION_RATIO_THRESHOLD = "OPENRECALL_WHISPER_COMPRESSION_RATIO_THRESHOLD"
ENV_WHISPER_CONDITION_ON_PREVIOUS_TEXT = "OPENRECALL_WHISPER_CONDITION_ON_PREVIOUS_TEXT"
ENV_WHISPER_HALLUCINATION_BLOCKLIST_ENABLED = "OPENRECALL_WHISPER_HALLUCINATION_BLOCKLIST_ENABLED"
ENV_WHISPER_HALLUCINATION_MAX_WORDS = "OPENRECALL_WHISPER_HALLUCINATION_MAX_WORDS"
ENV_WHISPER_HALLUCINATION_PHRASES = "OPENRECALL_WHISPER_HALLUCINATION_PHRASES"
ENV_WHISPER_VAD_MODE = "OPENRECALL_WHISPER_VAD_MODE"
ENV_WHISPER_VAD_AGGRESSIVENESS = "OPENRECALL_WHISPER_VAD_AGGRESSIVENESS"

# The only supported spectral-VAD backend today. ``vad_mode`` is None (off)
# by default because webrtcvad is an optional, install-separately dependency
# and at high aggressiveness it can drop quiet real speech — both reasons to
# leave it opt-in rather than always-on.
_VAD_MODES = ("webrtc",)

# --- ASR backend selection --------------------------------------------------
# Which streaming transcription backend the gateway builds. ``whisper`` (the
# default) is the existing mlx-whisper path — unchanged behavior. ``parakeet``
# switches to NVIDIA Parakeet-TDT via parakeet-mlx, whose transducer decoder
# emits a "blank" symbol on silence and so structurally avoids the
# autoregressive hallucinations ("Thank you.", "Hello.") that Whisper
# produces on near-silent / low-SNR audio. The switch is one env var so an
# operator can flip back trivially (unset it -> whisper).
ENV_ASR_BACKEND = "OPENRECALL_ASR_BACKEND"
ENV_PARAKEET_MODEL = "OPENRECALL_PARAKEET_MODEL"
ENV_ASR_MODE = "OPENRECALL_ASR_MODE"
_ASR_BACKENDS = ("whisper", "parakeet")

# Transcription scheduling mode. "utterance": buffer speech and transcribe
# each utterance once when the wearer pauses — no overlapping re-transcription
# (real-device evidence 2026-08-29: Parakeet's token timestamps shift between
# overlapping rolling-window calls, defeating the streaming dedup and
# interleaving duplicate fragments; the same audio batch-transcribes cleanly).
# "hop": the original rolling-window streaming path. "auto": utterance for
# parakeet, hop for whisper (whisper's word timestamps are stable enough for
# the streaming dedup, and its filters are tuned for that path).
_ASR_MODES = ("auto", "utterance", "hop")

# --- command detector --------------------------------------------------------
ENV_COMMAND_ENABLED = "OPENRECALL_COMMAND_DETECTOR_ENABLED"
ENV_COMMAND_REQUIRE_WEARER = "OPENRECALL_COMMAND_REQUIRE_WEARER"
ENV_COMMAND_CONFIDENCE_THRESHOLD = "OPENRECALL_COMMAND_CONFIDENCE_THRESHOLD"
ENV_COMMAND_COOLDOWN_S = "OPENRECALL_COMMAND_COOLDOWN_S"
ENV_COMMAND_MAX_INFLIGHT = "OPENRECALL_COMMAND_MAX_INFLIGHT"
ENV_COMMAND_LLM_MODEL = "OPENRECALL_COMMAND_LLM_MODEL"
ENV_COMMAND_LLM_BASE_URL = "OPENRECALL_COMMAND_LLM_BASE_URL"
ENV_COMMAND_LLM_API_KEY = "OPENRECALL_COMMAND_LLM_API_KEY"
ENV_COMMAND_PHRASES = "OPENRECALL_COMMAND_PHRASES"

# Day-one voice vocabulary: phrase -> device command type. Stage 1 matches
# these phrases (lowercased substring) against a rolling recent-text buffer.
# Bare common words ("stop", "start") are deliberately NOT keys — they appear
# constantly in speech and would defeat the filter.
DEFAULT_COMMAND_PHRASES: dict[str, str] = {
    "take a photo": "capture_photo",
    "take a picture": "capture_photo",
    "snap a photo": "capture_photo",
    "snap a pic": "capture_photo",
    "capture a photo": "capture_photo",
    "record a video": "record_video",
    "start a video": "start_video",
    "start video": "start_video",
    "stop video": "stop_video",
    "stop the video": "stop_video",
    "start audio": "start_audio",
    "start recording audio": "start_audio",
    "stop audio": "stop_audio",
    "flush snapshots": "flush_snapshots",
}


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

    Whisper has a *third* failure mode the confidence gates above do
    not catch: on near-silent / low-SNR audio it autoregressively emits
    a short, confident training phrase — "Thank you.", "Hello.",
    "Thanks for watching." — with good ``no_speech_prob`` /
    ``avg_logprob``, so it passes the filter and gets stored. The
    fields below add three defenses, all env-tunable:

    - ``hallucination_blocklist`` (+ ``hallucination_max_words`` /
      ``hallucination_phrases``): drop a *short* segment whose text
      matches a known phantom phrase. Gated on word count so a real
      sentence that happens to contain "thank you" is kept.
    - ``compression_ratio_threshold`` (+ ``condition_on_previous_text``):
      pass through to mlx-whisper's own gzip-ratio fallback, and stop
      a hop's hallucinated output from being fed back as the next
      hop's prompt (the mlx default of ``True`` propagates phantoms
      across the rolling window).
    - ``vad_mode`` (+ ``vad_aggressiveness``): an opt-in spectral VAD
      (webrtcvad) that skips the Whisper call entirely on pure-noise
      hops. Off by default — webrtcvad is an optional dependency and
      can drop quiet real speech at high aggressiveness.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    no_speech_threshold: float = 0.6
    logprob_threshold: float = -1.0
    compression_ratio_threshold: float = 2.4
    condition_on_previous_text: bool = False
    hallucination_blocklist_enabled: bool = True
    hallucination_max_words: int = 4
    # ``None`` means "use the backend's built-in default phrase set";
    # an explicit tuple (possibly empty) overrides it.
    hallucination_phrases: tuple[str, ...] | None = None
    vad_mode: str | None = None
    vad_aggressiveness: int = 3

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
        if self.compression_ratio_threshold <= 0.0:
            raise ValueError(
                f"{ENV_WHISPER_COMPRESSION_RATIO_THRESHOLD}="
                f"{self.compression_ratio_threshold} must be > 0.0"
            )
        if self.hallucination_max_words < 1:
            raise ValueError(
                f"{ENV_WHISPER_HALLUCINATION_MAX_WORDS}="
                f"{self.hallucination_max_words} must be >= 1"
            )
        if self.vad_mode is not None and self.vad_mode not in _VAD_MODES:
            raise ValueError(
                f"{ENV_WHISPER_VAD_MODE}={self.vad_mode!r} must be one of "
                f"{list(_VAD_MODES)} or unset"
            )
        if not (0 <= self.vad_aggressiveness <= 3):
            raise ValueError(
                f"{ENV_WHISPER_VAD_AGGRESSIVENESS}={self.vad_aggressiveness} "
                f"must be in [0, 3]"
            )
        return self


class AsrConfig(BaseModel):
    """Which streaming ASR backend the gateway builds, plus backend-specific
    model overrides.

    ``backend`` selects the transcription engine:

    - ``"whisper"`` (the default): the existing mlx-whisper path
      (:class:`WhisperStreamingBackend`). Selecting this leaves the system
      exactly as it was before the Parakeet option existed — every
      Whisper-specific noise filter in :class:`WhisperConfig` applies.
    - ``"parakeet"``: NVIDIA Parakeet-TDT via parakeet-mlx
      (:class:`ParakeetStreamingBackend`). The TDT transducer emits a "blank"
      symbol during silence, so it structurally avoids the autoregressive
      near-silence hallucinations that Whisper's blocklist/dedup only patch
      over; the Whisper-specific no_speech/logprob/compression/blocklist
      filters do not apply (they operate on mlx-whisper segment metadata
      that Parakeet does not produce). The backend-agnostic cross-hop dedup
      and the optional webrtcvad gate in :class:`StreamingTranscriber` still
      protect it.

    ``parakeet_model`` is the HuggingFace repo id for the Parakeet backend
    (ignored when ``backend="whisper"``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    backend: str = "whisper"
    parakeet_model: str = "mlx-community/parakeet-tdt-0.6b-v3"
    # Transcription scheduling: "auto" resolves per backend (utterance for
    # parakeet, hop for whisper) — see _ASR_MODES and resolved_mode().
    mode: str = "auto"

    @model_validator(mode="after")
    def _validate_backend(self) -> "AsrConfig":
        if self.backend not in _ASR_BACKENDS:
            raise ValueError(
                f"{ENV_ASR_BACKEND}={self.backend!r} must be one of "
                f"{list(_ASR_BACKENDS)}"
            )
        if self.mode not in _ASR_MODES:
            raise ValueError(
                f"{ENV_ASR_MODE}={self.mode!r} must be one of {list(_ASR_MODES)}"
            )
        if not self.parakeet_model.strip():
            raise ValueError(
                f"{ENV_PARAKEET_MODEL}={self.parakeet_model!r} must be a "
                f"non-empty HuggingFace repo id"
            )
        return self

    def resolved_mode(self) -> str:
        """The effective scheduling mode: "utterance" or "hop"."""
        if self.mode != "auto":
            return self.mode
        return "utterance" if self.backend == "parakeet" else "hop"


class CommandDetectorConfig(BaseModel):
    """Configuration for the speech -> command channel: detecting device
    commands ("take a photo", "start a video") from the rolling transcript
    and forwarding them to the firmware.

    The detector is OFF by default (``enabled=False``); an operator opts in
    via ``OPENRECALL_COMMAND_DETECTOR_ENABLED=true``. ``require_wearer``
    gates command execution on the wearer being an enrolled speaker so a
    bystander cannot trigger capture — on by default, relax only if you
    accept that risk.

    ``phrases`` is the phrase -> command-type map the Stage-1 substring
    matcher scans the recent-text buffer against. When
    ``OPENRECALL_COMMAND_PHRASES`` is unset the :data:`DEFAULT_COMMAND_PHRASES`
    ship; an empty string is a deliberate "no phrases" override (-> {}).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: bool = False
    require_wearer: bool = True
    confidence_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    cooldown_s: float = Field(default=3.0, ge=0.0)
    max_inflight: int = Field(default=1, ge=1)
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    phrases: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_COMMAND_PHRASES)
    )


class AgentConfig(BaseModel):
    """The full server config — guardrails + whisper noise filtering + ASR
    backend selection + command-detector policy. Future policy (extraction
    interval, etc.) lives here."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    whisper: WhisperConfig = Field(default_factory=WhisperConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    command: CommandDetectorConfig = Field(default_factory=CommandDetectorConfig)


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


_TRUE_STRINGS = {"1", "true", "yes", "on"}
_FALSE_STRINGS = {"0", "false", "no", "off"}


def _parse_bool(name: str, raw: str) -> bool:
    norm = raw.strip().lower()
    if norm in _TRUE_STRINGS:
        return True
    if norm in _FALSE_STRINGS:
        return False
    raise ValueError(
        f"{name}={raw!r} is not a valid boolean (use one of "
        f"{sorted(_TRUE_STRINGS | _FALSE_STRINGS)})"
    )


def _parse_phrase_list(name: str, raw: str) -> tuple[str, ...]:
    """Comma-separated phrases -> stripped tuple. Empty string -> empty tuple
    (a deliberate "no custom phrases" override, distinct from the None default
    that means "use the built-in set")."""
    parts = [p.strip() for p in raw.split(",")]
    # Drop empty trailing entries (e.g. "a, b," -> ("a", "b")) but keep a
    # legitimately empty override ("") -> ().
    if not raw.strip():
        return ()
    return tuple(p for p in parts if p)


def _parse_phrase_map(name: str, raw: str) -> dict[str, str]:
    """Semicolon-separated ``type:phrase`` pairs -> phrase -> type dict.
    An empty string is a deliberate "no phrases" override (-> {})."""
    raw = raw.strip()
    if not raw:
        return {}
    out: dict[str, str] = {}
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise ValueError(
                f"{name}={raw!r}: each entry must be 'type:phrase' (got {pair!r})"
            )
        ctype, phrase = pair.split(":", 1)
        ctype = ctype.strip()
        phrase = phrase.strip()
        if not ctype or not phrase:
            raise ValueError(f"{name}={raw!r}: empty type or phrase in {pair!r}")
        out[phrase.lower()] = ctype
    return out


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
    if ENV_WHISPER_COMPRESSION_RATIO_THRESHOLD in env:
        whisper_kwargs["compression_ratio_threshold"] = _parse_float(
            ENV_WHISPER_COMPRESSION_RATIO_THRESHOLD,
            env[ENV_WHISPER_COMPRESSION_RATIO_THRESHOLD],
        )
    if ENV_WHISPER_CONDITION_ON_PREVIOUS_TEXT in env:
        whisper_kwargs["condition_on_previous_text"] = _parse_bool(
            ENV_WHISPER_CONDITION_ON_PREVIOUS_TEXT,
            env[ENV_WHISPER_CONDITION_ON_PREVIOUS_TEXT],
        )
    if ENV_WHISPER_HALLUCINATION_BLOCKLIST_ENABLED in env:
        whisper_kwargs["hallucination_blocklist_enabled"] = _parse_bool(
            ENV_WHISPER_HALLUCINATION_BLOCKLIST_ENABLED,
            env[ENV_WHISPER_HALLUCINATION_BLOCKLIST_ENABLED],
        )
    if ENV_WHISPER_HALLUCINATION_MAX_WORDS in env:
        whisper_kwargs["hallucination_max_words"] = _parse_int(
            ENV_WHISPER_HALLUCINATION_MAX_WORDS,
            env[ENV_WHISPER_HALLUCINATION_MAX_WORDS],
        )
    if ENV_WHISPER_HALLUCINATION_PHRASES in env:
        whisper_kwargs["hallucination_phrases"] = _parse_phrase_list(
            ENV_WHISPER_HALLUCINATION_PHRASES,
            env[ENV_WHISPER_HALLUCINATION_PHRASES],
        )
    if ENV_WHISPER_VAD_MODE in env:
        raw_mode = env[ENV_WHISPER_VAD_MODE].strip()
        whisper_kwargs["vad_mode"] = raw_mode or None
    if ENV_WHISPER_VAD_AGGRESSIVENESS in env:
        whisper_kwargs["vad_aggressiveness"] = _parse_int(
            ENV_WHISPER_VAD_AGGRESSIVENESS,
            env[ENV_WHISPER_VAD_AGGRESSIVENESS],
        )
    asr_kwargs: dict = {}
    if ENV_ASR_BACKEND in env:
        # Case/whitespace-insensitive so OPENRECALL_ASR_BACKEND="Parakeet "
        # works. An empty value means "unset" -> the whisper default, so an
        # operator can revert by blanking the var as well as by removing it.
        raw_backend = env[ENV_ASR_BACKEND].strip().lower()
        if raw_backend:
            asr_kwargs["backend"] = raw_backend
    if ENV_PARAKEET_MODEL in env:
        raw_model = env[ENV_PARAKEET_MODEL].strip()
        if raw_model:
            asr_kwargs["parakeet_model"] = raw_model
    if ENV_ASR_MODE in env:
        raw_mode = env[ENV_ASR_MODE].strip().lower()
        if raw_mode:
            asr_kwargs["mode"] = raw_mode
    command_kwargs: dict = {}
    if ENV_COMMAND_ENABLED in env:
        command_kwargs["enabled"] = _parse_bool(
            ENV_COMMAND_ENABLED, env[ENV_COMMAND_ENABLED]
        )
    if ENV_COMMAND_REQUIRE_WEARER in env:
        command_kwargs["require_wearer"] = _parse_bool(
            ENV_COMMAND_REQUIRE_WEARER, env[ENV_COMMAND_REQUIRE_WEARER]
        )
    if ENV_COMMAND_CONFIDENCE_THRESHOLD in env:
        command_kwargs["confidence_threshold"] = _parse_float(
            ENV_COMMAND_CONFIDENCE_THRESHOLD,
            env[ENV_COMMAND_CONFIDENCE_THRESHOLD],
        )
    if ENV_COMMAND_COOLDOWN_S in env:
        command_kwargs["cooldown_s"] = _parse_float(
            ENV_COMMAND_COOLDOWN_S, env[ENV_COMMAND_COOLDOWN_S]
        )
    if ENV_COMMAND_MAX_INFLIGHT in env:
        command_kwargs["max_inflight"] = _parse_int(
            ENV_COMMAND_MAX_INFLIGHT, env[ENV_COMMAND_MAX_INFLIGHT]
        )
    if ENV_COMMAND_LLM_MODEL in env:
        raw_model = env[ENV_COMMAND_LLM_MODEL].strip()
        if raw_model:
            command_kwargs["llm_model"] = raw_model
    if ENV_COMMAND_LLM_BASE_URL in env:
        raw_url = env[ENV_COMMAND_LLM_BASE_URL].strip()
        if raw_url:
            command_kwargs["llm_base_url"] = raw_url
    if ENV_COMMAND_LLM_API_KEY in env:
        raw_key = env[ENV_COMMAND_LLM_API_KEY].strip()
        if raw_key:
            command_kwargs["llm_api_key"] = raw_key
    if ENV_COMMAND_PHRASES in env:
        command_kwargs["phrases"] = _parse_phrase_map(
            ENV_COMMAND_PHRASES, env[ENV_COMMAND_PHRASES]
        )
    return AgentConfig(
        guardrails=GuardrailsConfig(**guardrails_kwargs),
        whisper=WhisperConfig(**whisper_kwargs),
        asr=AsrConfig(**asr_kwargs),
        command=CommandDetectorConfig(**command_kwargs),
    )
