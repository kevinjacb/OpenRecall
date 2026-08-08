"""Speaker-recognition config — env-driven, validated at startup.

One frozen seam for every ``OPENSAPIEN_SPEAKER_*`` number. Loaded once at startup; a
bad value raises so the gateway refuses to start rather than running with the
wrong policy. Mirrors agent/config.py.
"""
from __future__ import annotations

from typing import Mapping

from pydantic import BaseModel, ConfigDict, model_validator

ENV_ENABLED = "OPENSAPIEN_SPEAKER_ENABLED"


def _parse_bool(name: str, raw: str) -> bool:
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name}={raw!r} is not a valid boolean")


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


class SpeakerConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    embed_model: str = ""
    embed_base_url: str | None = None
    embed_api_key: str | None = None

    confirm_threshold: float = 0.78
    tentative_threshold: float = 0.70
    min_speech_ms: int = 500
    cluster_threshold: float = 0.65
    corroborate_n: int = 3
    corroborate_window_s: int = 30
    pending_ttl_s: int = 60
    ring_buffer_n: int = 100
    outlier_trim_pct: float = 0.1
    ema_alpha: float = 0.05
    coldstart_window_s: int = 120
    confirm_turns: int = 10
    name_nudge_turns: int = 8
    min_confidence: float = 0.5

    @model_validator(mode="after")
    def _validate_bounds(self) -> "SpeakerConfig":
        for name in (
            "confirm_threshold", "tentative_threshold", "cluster_threshold",
            "outlier_trim_pct", "ema_alpha", "min_confidence",
        ):
            v = getattr(self, name)
            if not (0.0 <= v <= 1.0):
                raise ValueError(f"{name}={v} must be in [0.0, 1.0]")
        if self.confirm_threshold <= self.tentative_threshold:
            raise ValueError(
                f"confirm_threshold ({self.confirm_threshold}) must be > "
                f"tentative_threshold ({self.tentative_threshold})"
            )
        if self.cluster_threshold >= self.tentative_threshold:
            raise ValueError(
                f"cluster_threshold ({self.cluster_threshold}) must be < "
                f"tentative_threshold ({self.tentative_threshold}); otherwise a "
                f"distinct voice in [cluster, tentative) is tentatively absorbed "
                f"into an existing speaker instead of clustering"
            )
        for name in (
            "min_speech_ms", "corroborate_n", "corroborate_window_s",
            "pending_ttl_s", "ring_buffer_n", "coldstart_window_s",
            "confirm_turns", "name_nudge_turns",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name}={getattr(self, name)} must be > 0")
        return self


def load_speaker_config(env: Mapping[str, str]) -> SpeakerConfig:
    """Build the :class:`SpeakerConfig` from a process environment.

    Call once at startup; pass the result to every configurable consumer. A bad
    value raises with a clear message; the gateway should refuse to start rather
    than run with the wrong policy.
    """
    kwargs: dict = {}
    if ENV_ENABLED in env:
        kwargs["enabled"] = _parse_bool(ENV_ENABLED, env[ENV_ENABLED])
    if "OPENSAPIEN_SPEAKER_EMBED_MODEL" in env:
        kwargs["embed_model"] = env["OPENSAPIEN_SPEAKER_EMBED_MODEL"]
    if "OPENSAPIEN_SPEAKER_EMBED_BASE_URL" in env:
        kwargs["embed_base_url"] = env["OPENSAPIEN_SPEAKER_EMBED_BASE_URL"]
    if "OPENSAPIEN_SPEAKER_EMBED_API_KEY" in env:
        kwargs["embed_api_key"] = env["OPENSAPIEN_SPEAKER_EMBED_API_KEY"]
    for f in (
        "confirm_threshold", "tentative_threshold", "cluster_threshold",
        "ema_alpha", "outlier_trim_pct", "min_confidence",
    ):
        envn = f"OPENSAPIEN_SPEAKER_{f.upper()}"
        if envn in env:
            kwargs[f] = _parse_float(envn, env[envn])
    for f in (
        "min_speech_ms", "corroborate_n", "corroborate_window_s",
        "pending_ttl_s", "ring_buffer_n", "coldstart_window_s",
        "confirm_turns", "name_nudge_turns",
    ):
        envn = f"OPENSAPIEN_SPEAKER_{f.upper()}"
        if envn in env:
            kwargs[f] = _parse_int(envn, env[envn])
    return SpeakerConfig(**kwargs)