"""Vision keyframe tuning — ``OPENRECALL_VISION_*`` env knobs (P4b).

Internal tuning for scene-change keyframe selection (not user-facing like
``snapshot_interval_s``), so it follows the ``OPENRECALL_VLM_*`` /
``OPENRECALL_WHISPER_*`` env-var pattern, not ``SettingsDocument`` fields.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


def _fenv(env: Mapping[str, str], key: str, default: float, *, lo: float = 0.0) -> float:
    raw = env.get(key)
    if raw is None:
        return default
    val = float(raw)  # raises ValueError on bad input (caller lets it propagate)
    if val < lo:
        raise ValueError(f"{key} must be >= {lo}, got {val}")
    return val


def _ienv(env: Mapping[str, str], key: str, default: int, *, lo: int = 0) -> int:
    raw = env.get(key)
    if raw is None:
        return default
    val = int(raw)
    if val < lo:
        raise ValueError(f"{key} must be >= {lo}, got {val}")
    return val


@dataclass(frozen=True)
class VisionConfig:
    keyframe_threshold: float = 0.12   # normalized MAD (mean abs diff / 255) vs last keyframe
    keyframe_min_gap: int = 5           # cooldown: frames between selections
    keyframe_cap: int = 12              # max scene atoms per clip (bounds VLM cost)
    keyframe_thumb_size: int = 32      # NxN grayscale thumbnail for the diff

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "VisionConfig":
        return cls(
            keyframe_threshold=_fenv(env, "OPENRECALL_VISION_KEYFRAME_THRESHOLD", 0.12),
            keyframe_min_gap=_ienv(env, "OPENRECALL_VISION_KEYFRAME_MIN_GAP", 5),
            keyframe_cap=_ienv(env, "OPENRECALL_VISION_KEYFRAME_CAP", 12, lo=1),
            keyframe_thumb_size=_ienv(env, "OPENRECALL_VISION_KEYFRAME_THUMB_SIZE", 32, lo=4),
        )
