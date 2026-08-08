package com.opensapien.relay.http.dto

import com.opensapien.relay.protocol.Wire
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * Wire-format mirror of [com.opensapien.relay.domain.model.SessionSummary].
 * Field names are the server's JSON keys; the mapper
 * ([com.opensapien.relay.http.dto.toDomain]) is the ONLY path from this DTO
 * to a domain type. Keep DTOs dumb (data + serializer) so the wire
 * format can drift without bleeding into the UI.
 *
 * `startedAt` and `endedAt` are ISO-8601 strings; the mapper parses
 * them into [java.time.Instant]. `preview` may be empty.
 */
@Serializable
data class SessionSummaryDto(
    val id: String,
    val startedAt: String,
    val endedAt: String? = null,
    val durationMs: Long,
    val transcriptCount: Int,
    val preview: String,
)

@Serializable
data class SessionDetailsDto(
    val summary: SessionSummaryDto,
    val events: List<CaptureEventDto>,
)

/**
 * One event from the per-session timeline. The wire format carries
 * `kind` (today: `"transcript"`; future: `"audio"`, …) and a body
 * keyed by kind. The mapper dispatches on `kind` and ignores unknown
 * kinds (calling the warning callback so a Phase 3 server can grow
 * kinds without crashing the app).
 */
@Serializable
data class CaptureEventDto(
    val id: String,
    val sessionId: String,
    val seq: Int,
    val startMs: Long,
    val createdAt: String,
    val kind: String,
    // The body is open-ended per-kind: the mapper reads the keys it
    // knows and ignores the rest. Wire.json has ignoreUnknownKeys=true
    // so extra fields don't deserialization-fail.
    val text: String = "",
    val durationMs: Int = 0,
    val codec: String = "",
    val sampleRateHz: Int = 0,
    val byteCount: Int = 0,
    // Speaker fields resolved at read time by the server. Additive; default
    // so an older server's payloads still deserialize. Biometrics are
    // NEVER sent over HTTP (centroid/embedding_model/dim are server-only).
    val speaker: String? = null,
    val speakerName: String? = null,
    val isWearer: Boolean = false,
    val speakerConfidence: Double? = null,
    val speakerAssignment: String? = null,
)

@Serializable
data class SpeakerDto(
    val speakerId: String,
    val displayName: String? = null,
    val isWearer: Boolean = false,
    val enrollmentStatus: String = "",
    val turnCount: Int = 0,
    val firstSeen: String = "",
    val updatedAt: String = "",
)

@Serializable
data class SpeakersDto(val speakers: List<SpeakerDto> = emptyList())

@Serializable
data class RenameSpeakerRequestDto(val name: String)

@Serializable
data class ReassignSpeakerRequestDto(
    val fromSpeakerId: String,
    val toSpeakerId: String,
    val scope: String = "all",
)

@Serializable
data class RenameSpeakerResponseDto(val speaker: SpeakerDto)

@Serializable
data class ServerStatusDto(
    val reachable: Boolean,
    val authenticated: Boolean,
    val version: String? = null,
    val uptimeSeconds: Long = 0,
    val activeSessions: Int = 0,
    val totalSessions: Int = 0,
    val recentEvents24h: Int = 0,
)

/**
 * The `GET /sessions` response body. `nextCursor` is null when the
 * server has no more pages — distinct from a non-null cursor with
 * an empty list (which means "fetch the next page; it just happens
 * to be empty").
 */
@Serializable
data class SessionsPageDto(
    val sessions: List<SessionSummaryDto>,
    val nextCursor: String? = null,
)

/**
 * The `GET /sessions/{id}/events` response body — a list wrapper so the
 * server can grow sibling fields (paging, totals) without changing the
 * shape. `events` defaults to empty so a `{}` body still deserializes.
 */
@Serializable
data class SessionEventsDto(
    val events: List<CaptureEventDto> = emptyList(),
)

/**
 * Shared `Json` instance for DTO parsing/encoding. Matches the
 * project's [com.opensapien.relay.protocol.Wire.json] settings (lenient
 * + default encoding) so a server field addition never deserializes
 * to a hard failure.
 */
internal val DtoJson: Json = Wire.json

/** Read a string field from a [JsonObject] without crashing. */
internal fun JsonObject.str(k: String): String? =
    runCatching { (this[k] as? JsonElement)?.jsonPrimitive?.content }.getOrNull()
