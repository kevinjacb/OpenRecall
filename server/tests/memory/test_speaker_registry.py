"""Tests for the SpeakerRegistry — tables, ring buffer, outlier-trim + EMA centroid.

Runs against both backends (in-memory + sqlite) via the ``reg`` fixture, so the
durable backend can't silently diverge. Mirrors the EventStore/AtomStore suite.
"""
from __future__ import annotations

import math
import threading

import pytest

from openrecall_server.ingest.speaker_config import SpeakerConfig
from openrecall_server.memory.speaker_registry import (
    InMemorySpeakerRegistry,
    Speaker,
    SqliteSpeakerRegistry,
)


def _unit(dim: int, seed: float) -> list[float]:
    v = [seed] * dim
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


@pytest.fixture(params=["memory", "sqlite"])
def reg(request, tmp_path):
    cfg = SpeakerConfig()
    if request.param == "memory":
        return InMemorySpeakerRegistry(cfg)
    return SqliteSpeakerRegistry(tmp_path / "speakers.db", cfg)


def _new_speaker(reg, speaker_id="s1", is_wearer=False):
    reg.add_speaker(Speaker(
        speaker_id=speaker_id, display_name=None, is_wearer=is_wearer,
        enrollment_status="confirmed", centroid=None, embedding_model="m",
        dim=4, turn_count=0, first_seen="2026-07-26T00:00:00+00:00",
        updated_at="2026-07-26T00:00:00+00:00",
    ))


def test_add_and_get_speaker(reg):
    _new_speaker(reg)
    s = reg.get("s1")
    assert s is not None and s.speaker_id == "s1"


def test_add_confirmed_embedding_appends_to_ring_buffer(reg):
    _new_speaker(reg)
    reg.add_confirmed_embedding("s1", _unit(4, 0.5), 0.9)
    reg.add_confirmed_embedding("s1", _unit(4, 0.6), 0.9)
    buf = reg.ring_buffer("s1")
    assert len(buf) == 2
    assert len(buf[0]) == 4


def test_recompute_centroid_is_ema_toward_trimmed_mean(reg):
    _new_speaker(reg)
    # three identical embeddings -> centroid == that unit vector
    v = _unit(4, 0.5)
    for _ in range(3):
        reg.add_confirmed_embedding("s1", v, 0.9)
    reg.recompute_centroid("s1")
    c = reg.centroid("s1")
    assert c is not None
    assert all(abs(a - b) < 1e-6 for a, b in zip(c, v))


def test_ring_buffer_caps_at_n(reg):
    _new_speaker(reg)
    v = _unit(4, 0.5)
    for _ in range(150):
        reg.add_confirmed_embedding("s1", v, 0.9)
    assert len(reg.ring_buffer("s1")) == 100  # ring_buffer_n


def test_outlier_trim_drops_farthest(reg):
    _new_speaker(reg)
    base = _unit(4, 0.5)
    # add 9 base + 1 wild outlier; trim 10% (~1) should drop the outlier
    for _ in range(9):
        reg.add_confirmed_embedding("s1", base, 0.9)
    reg.add_confirmed_embedding("s1", _unit(4, 0.99), 0.9)
    reg.recompute_centroid("s1")
    c = reg.centroid("s1")
    # centroid stays close to base, not pulled toward the outlier
    assert all(abs(a - b) < 0.05 for a, b in zip(c, base))


def test_increment_turn_returns_new_count(reg):
    _new_speaker(reg)
    assert reg.increment_turn("s1") == 1
    assert reg.increment_turn("s1") == 2


def test_set_display_name_and_update_enrollment(reg):
    _new_speaker(reg, is_wearer=True)
    reg.update_enrollment("s1", "implicit")
    reg.set_display_name("s1", "You")
    s = reg.get("s1")
    assert s.display_name == "You"
    assert s.enrollment_status == "implicit"


def test_sqlite_registry_is_thread_safe(tmp_path):
    reg = SqliteSpeakerRegistry(tmp_path / "speakers.db", SpeakerConfig())
    _new_speaker(reg)
    errs: list = []

    def worker():
        try:
            for _ in range(20):
                reg.add_confirmed_embedding("s1", _unit(4, 0.5), 0.9)
        except Exception as e:
            errs.append(e)

    ts = [threading.Thread(target=worker) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs

def test_set_is_wearer_updates_flag(reg):
    _new_speaker(reg, is_wearer=False)
    assert reg.get("s1").is_wearer is False
    reg.set_is_wearer("s1", True)
    assert reg.get("s1").is_wearer is True
    # idempotent flip back
    reg.set_is_wearer("s1", False)
    assert reg.get("s1").is_wearer is False
