package com.openrecall.relay.http.dto

import com.openrecall.relay.domain.model.AudioSegment
import com.openrecall.relay.domain.model.CaptureEvent
import com.openrecall.relay.domain.model.CaptureSettings
import com.openrecall.relay.domain.model.DeviceStatus
import com.openrecall.relay.domain.model.MemoryStats
import com.openrecall.relay.domain.model.RelaySettings
import com.openrecall.relay.domain.model.RetentionSettings
import com.openrecall.relay.domain.model.Segment
import com.openrecall.relay.domain.model.SegmentDetails
import com.openrecall.relay.domain.model.SegmentId
import com.openrecall.relay.domain.model.SessionDetails
import com.openrecall.relay.domain.model.SessionId
import com.openrecall.relay.domain.model.SessionSummary
import com.openrecall.relay.domain.model.ServerStatus
import com.openrecall.relay.domain.model.TranscriptChunk
import com.openrecall.relay.domain.model.Waveform
import java.time.Instant
import java.time.OffsetDateTime
import java.time.format.DateTimeParseException

/**
 * The ONLY path from wire DTOs to domain models. Pure functions; no
 * Android imports. Keeping the mapper thin and total is what makes
 * the UI testable without a real server.
 *
 * `onUnknownKind` is called when a server emits a `kind` the mapper
 * doesn't know yet (e.g. `"image"` from a future server). For now
 * the only caller is a test (which passes a no-op); in production
 * Phase 4+ will route the warning to `Log.w` via the DI seam.
 */
fun SessionSummaryDto.toDomain(): SessionSummary = SessionSummary(
    id = SessionId(id),
    startedAt = parseInstant(startedAt),
    endedAt = endedAt?.let(::parseInstant),
    durationMs = durationMs,
    transcriptCount = transcriptCount,
    preview = preview,
)

fun SessionDetailsDto.toDomain(): SessionDetails =
    SessionDetails(summary = summary.toDomain(), events = emptyList())

fun List<CaptureEventDto>.toDomain(onUnknownKind: (String) -> Unit): List<CaptureEvent> =
    mapNotNull { it.toDomainOrNull(onUnknownKind) }

/**
 * Convert one event. Returns null for unknown kinds (after invoking
 * the warning callback). The mapper is intentionally non-throwing:
 * the server can grow kinds without crashing older clients.
 */
fun CaptureEventDto.toDomainOrNull(onUnknownKind: (String) -> Unit): CaptureEvent? = when (kind) {
    "transcript" -> TranscriptChunk(
        id = id,
        sessionId = SessionId(sessionId),
        seq = seq,
        startMs = startMs,
        createdAt = parseInstant(createdAt),
        text = text,
        durationMs = durationMs,
        speaker = speaker,
        speakerName = speakerName,
        isWearer = isWearer,
    )
    "audio" -> AudioSegment(
        id = id,
        sessionId = SessionId(sessionId),
        seq = seq,
        startMs = startMs,
        createdAt = parseInstant(createdAt),
        codec = codec,
        sampleRateHz = sampleRateHz,
        byteCount = byteCount,
    )
    else -> {
        onUnknownKind(kind)
        null
    }
}

fun SegmentSummaryDto.toDomain(): Segment = Segment(
    id = SegmentId(id),
    sessionId = SessionId(sessionId),
    title = title,
    startedAt = parseInstant(startedAt),
    endedAt = endedAt?.let(::parseInstant),
    durationMs = durationMs,
    transcriptCount = transcriptCount,
    memoryCount = memoryCount,
    preview = preview,
    hasAudio = hasAudio,
    closed = closed,
    matchSnippet = matchSnippet,
)

fun SegmentDetailsDto.toDomain(onUnknownKind: (String) -> Unit): SegmentDetails = SegmentDetails(
    summary = summary.toDomain(),
    events = events.toDomain(onUnknownKind).sortedBy { it.seq },
)

fun WaveformDto.toDomain(): Waveform = Waveform(
    // A zero bucket would make every playhead computation divide by zero, so
    // an absent/garbage value falls back to the server's documented stride.
    bucketMs = bucketMs.takeIf { it > 0 } ?: 500,
    peaks = peaks.map { it.coerceIn(0f, 1f) },
    durationMs = durationMs,
)

fun SettingsDocumentDto.toDomain(): RelaySettings = RelaySettings(
    capture = CaptureSettings(
        audioEnabled = capture.audio_enabled,
        saveAudio = capture.save_audio,
        visionEnabled = capture.vision_enabled,
    ),
    retention = RetentionSettings(audioDays = retention.audio_days),
)

fun RelaySettings.toDto(): SettingsDocumentDto = SettingsDocumentDto(
    capture = CaptureSettingsDto(
        audio_enabled = capture.audioEnabled,
        save_audio = capture.saveAudio,
        vision_enabled = capture.visionEnabled,
    ),
    retention = RetentionSettingsDto(audio_days = retention.audioDays),
)

/**
 * `source` is the server's honesty flag: `"device"` means the hardware
 * reported these numbers, anything else (today always `"static"`) means they
 * are placeholders. Mapping it to a boolean here keeps the string out of the
 * UI, which only ever needs to know whether to render the value as real.
 */
fun DeviceStatusDto.toDomain(): DeviceStatus = DeviceStatus(
    measured = source == "device",
    batteryPct = battery_pct,
    storageFreeBytes = storage_free_bytes,
    firmwareVersion = firmware_version,
    recording = recording,
    relayConnected = relay_connected,
    microphoneAvailable = microphone_available,
    cameraAvailable = camera_available,
    lastPacketAt = last_packet_at?.let(::parseInstant),
    lastPacketAgeS = last_packet_age_s,
    lastTranscriptAt = last_transcript_at?.let(::parseInstant),
    lastTranscriptAgeS = last_transcript_age_s,
)

fun MemoryStatsResponseDto.toDomain(): MemoryStats = MemoryStats(
    total = total,
    added24h = added_24h,
    byKind = by_kind,
)

fun ServerStatusDto.toDomain(): ServerStatus = ServerStatus(
    reachable = reachable,
    authenticated = authenticated,
    version = version,
    uptimeSeconds = uptimeSeconds,
    activeSessions = activeSessions,
    totalSessions = totalSessions,
    recentEvents24h = recentEvents24h,
)

/**
 * Parse an ISO-8601 instant from the server's wire format.
 *
 * The server emits an explicit offset — `datetime.isoformat()` on a
 * `timezone.utc` value produces `…+00:00` (and `…+00:00.123456` when
 * there are fractional seconds), NOT a `Z` suffix. We parse via
 * [OffsetDateTime] (which accepts both `Z` and `+00:00` and fractional
 * seconds on every Java/Android version) and convert to an [Instant].
 *
 * `Instant.parse` would be simpler, but it delegates to
 * `DateTimeFormatter.ISO_INSTANT`, which rejected explicit offsets
 * until JDK-8166138 (Java 13). Android API 26-30 ship a `java.time`
 * based on OpenJDK 8-9, so `Instant.parse("…+00:00")` throws there —
 * silently falling back to [Instant.EPOCH] (every timestamp rendering
 * as 1970-01-01). Using [OffsetDateTime] avoids that without a server
 * change.
 *
 * On any failure we fall back to [Instant.EPOCH] — the UI shows "no
 * date" patterns for invalid times, the right degradation for a single
 * corrupt row.
 */
private fun parseInstant(s: String): Instant = try {
    OffsetDateTime.parse(s).toInstant()
} catch (_: DateTimeParseException) {
    Instant.EPOCH
}
