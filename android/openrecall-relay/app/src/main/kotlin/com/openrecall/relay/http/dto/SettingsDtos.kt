package com.openrecall.relay.http.dto

import kotlinx.serialization.Serializable

/**
 * `GET` / `PUT /settings` (server spec §4.1) and `GET /device/status` (§5.1).
 *
 * Greenfield server surfaces, so snake_case — the DTO field names are the
 * JSON keys and a rename is a bug.
 *
 * The three capture toggles are deliberately not interchangeable:
 *
 * * [CaptureSettingsDto.audio_enabled] is **device-level desired state**. The
 *   server issues `start_audio`/`stop_audio` to converge the wearable on it
 *   and gates ingest as a backstop, so the toggle still means something while
 *   the device is offline.
 * * [CaptureSettingsDto.save_audio] is **server-level**: it controls only
 *   whether the frame log is written. The microphone keeps working and
 *   transcripts keep forming.
 * * [CaptureSettingsDto.vision_enabled] gates the vision pipeline.
 */
@Serializable
data class CaptureSettingsDto(
    val audio_enabled: Boolean = true,
    val save_audio: Boolean = true,
    val vision_enabled: Boolean = false,
)

@Serializable
data class RetentionSettingsDto(
    /** Days of *audio* kept. Transcripts, atoms and vectors are never swept —
     *  a deliberate tiered policy the Settings copy has to state plainly. */
    val audio_days: Int = 30,
)

/**
 * The full settings document. Also the `PUT` body: the server's `SettingsPatch`
 * accepts a full document, and sending the whole merged doc avoids a partial
 * patch racing a concurrent change into an inconsistent pair.
 *
 * The server sets `extra="forbid"`, so an unknown key is a 400 rather than a
 * setting the user believes they changed and did not — which means this DTO
 * must not grow fields the server doesn't know.
 */
@Serializable
data class SettingsDocumentDto(
    val schema_version: String = "v1",
    val capture: CaptureSettingsDto = CaptureSettingsDto(),
    val retention: RetentionSettingsDto = RetentionSettingsDto(),
)

/**
 * `GET /device/status`.
 *
 * The payload is split down the middle and the split is the point.
 * [battery_pct] is a fixed server-side placeholder — battery sensing does not
 * exist at any layer of this product — and [source] is how a client knows:
 * `"static"` means the device-reported numbers are not measured. The UI must
 * render them visibly provisional. Everything from [recording] down is
 * genuinely measured from the gateway.
 */
@Serializable
data class DeviceStatusDto(
    val schema_version: String = "v1",
    val source: String = "static",
    val battery_pct: Double? = null,
    val storage_free_bytes: Long? = null,
    val firmware_version: String? = null,
    val recording: Boolean = false,
    val relay_connected: Boolean = false,
    val microphone_available: Boolean = true,
    val camera_available: Boolean = false,
    val last_packet_at: String? = null,
    val last_packet_age_s: Double? = null,
    val last_transcript_at: String? = null,
    val last_transcript_age_s: Double? = null,
)
