"""Phase 5 — the retention sweep (spec §5.3, D8).

Tiered by design: audio expires, derived text is kept. These tests pin both
halves, because the second half is the surprising one and is the reason the
app's privacy copy has to say so explicitly.
"""
from __future__ import annotations

import os

import pytest

from opensapien_server.media.audio import AudioStore
from opensapien_server.media.retention import RetentionSweeper
from opensapien_server.settings.model import SettingsDocument
from opensapien_server.settings.store import InMemorySettingsStore

_NOW = 1_800_000_000.0
_DAY = 86_400.0


@pytest.fixture
def audio(tmp_path):
    return AudioStore(tmp_path / "audio")


def _sweeper(audio, *, days=30, now=_NOW):
    settings = InMemorySettingsStore(
        SettingsDocument.model_validate({"retention": {"audio_days": days}}),
    )
    return RetentionSweeper(
        audio_store=audio, settings_store=settings, now=lambda: now,
    )


def _age(audio, session_id, days):
    path = audio.log_path(session_id)
    when = _NOW - days * _DAY
    os.utime(path, (when, when))
    peaks = audio.peaks_path(session_id)
    if peaks.exists():
        os.utime(peaks, (when, when))


# ---- expiry -----------------------------------------------------------------


def test_audio_older_than_the_window_is_deleted(audio):
    audio.write_at("old", 0, [b"aa"], peaks=[10])
    _age(audio, "old", days=40)

    assert _sweeper(audio, days=30).sweep_once() == ["old"]
    assert audio.has("old") is False


def test_audio_inside_the_window_survives(audio):
    audio.write_at("recent", 0, [b"aa"])
    _age(audio, "recent", days=5)

    assert _sweeper(audio, days=30).sweep_once() == []
    assert audio.has("recent") is True


def test_only_expired_sessions_are_swept(audio):
    audio.write_at("old", 0, [b"aa"])
    audio.write_at("new", 0, [b"aa"])
    _age(audio, "old", days=40)
    _age(audio, "new", days=1)

    assert _sweeper(audio, days=30).sweep_once() == ["old"]
    assert audio.has("new") is True


def test_the_peak_file_goes_with_the_audio(audio):
    audio.write_at("old", 0, [b"aa"], peaks=[10])
    _age(audio, "old", days=40)

    _sweeper(audio, days=30).sweep_once()

    assert audio.peaks("old", 0, 1000) == []


def test_the_cached_ogg_goes_too(audio):
    """Otherwise the cache becomes the thing retention failed to delete."""
    audio.write_at("old", 0, [b"aa"])
    _age(audio, "old", days=40)
    cache = audio.log_path("old").parent / "seg"
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / "old_0.ogg"
    cached.write_bytes(b"OggS")

    _sweeper(audio, days=30).sweep_once()

    assert not cached.exists()


def test_a_shorter_window_expires_more(audio):
    audio.write_at("s1", 0, [b"aa"])
    _age(audio, "s1", days=10)

    assert _sweeper(audio, days=30).sweep_once() == []
    assert _sweeper(audio, days=7).sweep_once() == ["s1"]


# ---- the off switch ---------------------------------------------------------


def test_zero_days_disables_the_sweep_rather_than_purging(audio):
    """A zero in a retention field reads as "off" far more often than it
    reads as "purge now", and the destructive reading is unrecoverable."""
    audio.write_at("ancient", 0, [b"aa"])
    _age(audio, "ancient", days=9999)

    assert _sweeper(audio, days=0).sweep_once() == []
    assert audio.has("ancient") is True


# ---- robustness -------------------------------------------------------------


def test_an_empty_store_sweeps_nothing(audio):
    assert _sweeper(audio).sweep_once() == []


def test_one_bad_session_does_not_stop_the_others(audio):
    audio.write_at("good", 0, [b"aa"])
    audio.write_at("bad", 0, [b"aa"])
    _age(audio, "good", days=40)
    _age(audio, "bad", days=40)

    original = audio.delete

    def flaky(session_id):
        if session_id == "bad":
            raise OSError("permission denied")
        return original(session_id)

    audio.delete = flaky

    assert _sweeper(audio, days=30).sweep_once() == ["good"]
