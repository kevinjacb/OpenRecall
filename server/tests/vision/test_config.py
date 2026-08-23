"""VisionConfig — keyframe tuning knobs from OPENRECALL_VISION_* env (P4b)."""
from __future__ import annotations

import pytest

from openrecall_server.vision.config import VisionConfig


def test_defaults():
    cfg = VisionConfig.from_env({})
    assert cfg.keyframe_threshold == pytest.approx(0.12)
    assert cfg.keyframe_min_gap == 5
    assert cfg.keyframe_cap == 12
    assert cfg.keyframe_thumb_size == 32


def test_reads_env_overrides():
    cfg = VisionConfig.from_env({
        "OPENRECALL_VISION_KEYFRAME_THRESHOLD": "0.25",
        "OPENRECALL_VISION_KEYFRAME_MIN_GAP": "8",
        "OPENRECALL_VISION_KEYFRAME_CAP": "20",
        "OPENRECALL_VISION_KEYFRAME_THUMB_SIZE": "16",
    })
    assert cfg.keyframe_threshold == pytest.approx(0.25)
    assert cfg.keyframe_min_gap == 8
    assert cfg.keyframe_cap == 20
    assert cfg.keyframe_thumb_size == 16


def test_bad_env_value_raises():
    with pytest.raises(ValueError):
        VisionConfig.from_env({"OPENRECALL_VISION_KEYFRAME_THRESHOLD": "not-a-number"})


def test_negative_cap_raises():
    with pytest.raises(ValueError):
        VisionConfig.from_env({"OPENRECALL_VISION_KEYFRAME_CAP": "-1"})