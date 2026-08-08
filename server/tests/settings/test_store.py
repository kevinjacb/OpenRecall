"""Phase 4 — the settings document and its store (spec §4.1)."""
from __future__ import annotations

import pytest

from opensapien_server.settings.model import SettingsDocument, SettingsPatch
from opensapien_server.settings.store import (
    InMemorySettingsStore,
    SqliteSettingsStore,
)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemorySettingsStore()
    return SqliteSettingsStore(tmp_path / "settings.db")


# ---- defaults ---------------------------------------------------------------


def test_an_empty_store_returns_defaults(store):
    doc = store.get()

    assert doc.schema_version == "v1"
    assert doc.capture.audio_enabled is True
    assert doc.capture.save_audio is True
    assert doc.capture.vision_enabled is False
    assert doc.retention.audio_days == 30


def test_a_stored_document_round_trips(store):
    store.put(SettingsDocument.model_validate({
        "capture": {"audio_enabled": False, "save_audio": False, "vision_enabled": True},
        "retention": {"audio_days": 7},
    }))

    doc = store.get()

    assert doc.capture.audio_enabled is False
    assert doc.capture.vision_enabled is True
    assert doc.retention.audio_days == 7


# ---- merging ----------------------------------------------------------------


def test_a_partial_patch_leaves_other_fields_alone():
    current = SettingsDocument()
    patch = SettingsPatch.model_validate({"capture": {"audio_enabled": False}})

    merged = patch.merge_onto(current)

    assert merged.capture.audio_enabled is False
    assert merged.capture.save_audio is True  # untouched
    assert merged.retention.audio_days == 30


def test_an_empty_patch_changes_nothing():
    current = SettingsDocument.model_validate({"retention": {"audio_days": 7}})

    assert SettingsPatch().merge_onto(current) == current


def test_an_unknown_key_is_rejected():
    """A typo'd key must be an error, not a setting the user believes they
    changed and did not."""
    with pytest.raises(Exception):
        SettingsPatch.model_validate({"capture": {"audio_enabld": False}})


def test_an_unknown_top_level_section_is_rejected():
    with pytest.raises(Exception):
        SettingsPatch.model_validate({"captur": {}})


def test_retention_days_are_bounded():
    with pytest.raises(Exception):
        SettingsPatch.model_validate({"retention": {"audio_days": -1}})


# ---- device state -----------------------------------------------------------


def test_device_state_starts_empty(store):
    assert store.get_device_state() == {}


def test_device_state_round_trips(store):
    store.put_device_state({"audio_enabled": True})

    assert store.get_device_state() == {"audio_enabled": True}


def test_device_state_is_separate_from_desired_state(store):
    """Keeping both is what makes reconciliation possible: without a
    last-known value there is nothing to compare desired state against."""
    store.put(SettingsDocument.model_validate({"capture": {"audio_enabled": False}}))
    store.put_device_state({"audio_enabled": True})

    assert store.get().capture.audio_enabled is False
    assert store.get_device_state()["audio_enabled"] is True


# ---- durability -------------------------------------------------------------


def test_settings_survive_a_reopen(tmp_path):
    path = tmp_path / "settings.db"
    SqliteSettingsStore(path).put(
        SettingsDocument.model_validate({"capture": {"audio_enabled": False}}),
    )

    assert SqliteSettingsStore(path).get().capture.audio_enabled is False


def test_an_unreadable_row_falls_back_to_defaults(tmp_path):
    """The settings screen failing to load is worse than one stale value
    reverting; the next PUT heals it."""
    path = tmp_path / "settings.db"
    store = SqliteSettingsStore(path)
    store.put(SettingsDocument())
    store._write("document", {"capture": {"nonsense": 1}})

    assert SqliteSettingsStore(path).get() == SettingsDocument()
