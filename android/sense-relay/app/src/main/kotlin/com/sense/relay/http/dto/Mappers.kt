package com.sense.relay.http.dto

import com.sense.relay.domain.model.AudioSegment
import com.sense.relay.domain.model.CaptureEvent
import com.sense.relay.domain.model.SessionDetails
import com.sense.relay.domain.model.SessionId
import com.sense.relay.domain.model.SessionSummary
import com.sense.relay.domain.model.ServerStatus
import com.sense.relay.domain.model.TranscriptChunk
import java.time.Instant
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
 * Parse an ISO-8601 instant. Tolerant of the `Z` suffix the server
 * emits. On failure we fall back to `Instant.EPOCH` — the UI shows
 * "no date" patterns for invalid times, which is the right
 * degradation for a single corrupt row.
 */
private fun parseInstant(s: String): Instant = try {
    Instant.parse(s)
} catch (_: DateTimeParseException) {
    Instant.EPOCH
}
