"""The settings document (spec §4.1).

Storage is an open key-value table; the wire is a fixed, typed document. That
split is deliberate — adding a setting should not need a migration, but a
client should never have to guess whether ``audio_enabled`` is a bool, the
string ``"true"``, or absent.

The three capture toggles are **not** interchangeable, and the difference is
the whole point of D2:

* ``audio_enabled`` is device-level. It is desired state: the server issues
  ``start_audio``/``stop_audio`` to converge the device on it, and gates
  ingest as a backstop so the toggle is never merely cosmetic while the
  device is offline or ignoring commands.
* ``save_audio`` is server-level. It controls only whether ingest writes the
  frame log — the microphone keeps working and transcripts keep forming.
* ``vision_enabled`` gates the vision pipeline.

Note (spec §4.1): ``findings.md`` refers to "all three capture toggles"
without naming them, and one of the design's three was the wake-word toggle,
which is cut — there is no wake-word engine in the firmware and the wearable
is designed without one. These three are the honest set; the third should be
confirmed against the design file.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CaptureSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    audio_enabled: bool = True
    save_audio: bool = True
    vision_enabled: bool = False
    # P2: desired power state. True = the user wants the device asleep; the
    # reconciler converges it by issuing `sleep`. A button-wake telemetry
    # clears this (D2). Default False (awake) — see spec §2.6.
    sleep_mode: bool = False
    # P3: ambient snapshot cadence in seconds (0 = off). The reconciler
    # converges the device on it by issuing `set_snapshot_interval` (spec §3.4).
    # Slider range 10–600 in the app; 0 disables ambient capture.
    snapshot_interval_s: int = Field(default=60, ge=0, le=600)


class RetentionSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    # Days of *audio* to keep. Transcripts, atoms and vectors are not swept
    # (spec D8) — a deliberate tiered policy, not an oversight.
    audio_days: int = Field(default=30, ge=0, le=3650)
    # P3 (D8 extended): days of scene *image blobs* to keep. Scene atoms
    # (the caption text) are kept forever; only the bulky/sensitive image
    # is swept. 0 = keep indefinitely.
    snapshot_days: int = Field(default=30, ge=0, le=3650)


class SettingsDocument(BaseModel):
    """The full settings document, as returned by ``GET /settings``."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["v1"] = "v1"
    capture: CaptureSettings = Field(default_factory=CaptureSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)


class CapturePatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    audio_enabled: bool | None = None
    save_audio: bool | None = None
    vision_enabled: bool | None = None
    sleep_mode: bool | None = None
    snapshot_interval_s: int | None = Field(default=None, ge=0, le=600)


class RetentionPatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    audio_days: int | None = Field(default=None, ge=0, le=3650)
    snapshot_days: int | None = Field(default=None, ge=0, le=3650)


class SettingsPatch(BaseModel):
    """Inbound payload for ``PUT /settings`` — full or partial.

    ``extra="forbid"`` throughout, so a typo'd key is a 400 rather than a
    setting the user believes they changed and did not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["v1"] = "v1"
    capture: CapturePatch | None = None
    retention: RetentionPatch | None = None

    def merge_onto(self, current: SettingsDocument) -> SettingsDocument:
        """Apply this patch to ``current``, leaving unset fields untouched."""
        capture = current.capture
        if self.capture is not None:
            capture = capture.model_copy(
                update=self.capture.model_dump(exclude_none=True),
            )
        retention = current.retention
        if self.retention is not None:
            retention = retention.model_copy(
                update=self.retention.model_dump(exclude_none=True),
            )
        return SettingsDocument(capture=capture, retention=retention)
