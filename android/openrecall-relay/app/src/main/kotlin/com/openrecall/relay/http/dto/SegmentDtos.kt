package com.openrecall.relay.http.dto

import kotlinx.serialization.Serializable

/**
 * Wire DTOs for the `/segments` surface (server spec §2.3, §3.2, §3.3).
 *
 * A **segment** is not a session. A session is minted by [RelayService] when
 * the foreground service starts and is reused across every BLE/WS reconnect,
 * so it spans hours or days; a segment is one contiguous run of transcript
 * activity inside it, closed by a server-side idle timer. Segments are what
 * the Recordings list and the detail screen address — `/sessions` remains the
 * plumbing/debug surface and the app does not read it.
 *
 * camelCase, mirroring `/sessions` (the server extends a surface in the
 * convention it already uses). Every field carries a default so an older
 * server's payload still deserializes.
 */
@Serializable
data class SegmentSummaryDto(
    val id: String,
    val sessionId: String = "",
    /** LLM- or user-authored; null until the titler has run. */
    val title: String? = null,
    val startedAt: String,
    val endedAt: String? = null,
    val durationMs: Long = 0,
    val transcriptCount: Int = 0,
    val memoryCount: Int = 0,
    val preview: String = "",
    val hasAudio: Boolean = false,
    /** False while the segment is still being written to — the server refuses
     *  to delete an open segment and never caches its audio. */
    val closed: Boolean = true,
    /** Only present on `GET /segments?q=` rows: the matching transcript window. */
    val matchSnippet: String? = null,
)

@Serializable
data class SegmentsPageDto(
    val segments: List<SegmentSummaryDto> = emptyList(),
    /** Null on the last page — and always null in search mode, which the
     *  server serves unpaginated. */
    val nextCursor: String? = null,
)

@Serializable
data class SegmentDetailsDto(
    val summary: SegmentSummaryDto,
    val events: List<CaptureEventDto> = emptyList(),
)

/** `GET /segments/{id}/memory` — atoms whose `start_ms` falls in the segment's
 *  window. camelCase envelope around snake_case atoms, matching the server. */
@Serializable
data class SegmentMemoryDto(
    val atoms: List<MemoryAtomDto> = emptyList(),
    val returnedCount: Int = 0,
)

/**
 * `GET /segments/{id}/waveform` — peaks in fixed [bucketMs] buckets, each in
 * 0..1. Bucket `i` covers `[i*bucketMs, (i+1)*bucketMs)` of segment time with
 * gaps as 0, which is what lets the playhead line up with transcript
 * `startMs` without decoding audio.
 */
@Serializable
data class WaveformDto(
    val schemaVersion: String = "v1",
    val bucketMs: Int = 500,
    val peaks: List<Float> = emptyList(),
    val durationMs: Long = 0,
)

/** `PATCH /segments/{id}` — rename. The server stores `title_source="user"`,
 *  which permanently protects the title from the auto-titler. */
@Serializable
data class PatchSegmentRequestDto(val title: String)
