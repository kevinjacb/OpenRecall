package com.openrecall.relay.data

import com.openrecall.relay.domain.model.SegmentId
import com.openrecall.relay.http.dto.SegmentDetailsDto
import com.openrecall.relay.http.dto.SegmentMemoryDto
import com.openrecall.relay.http.dto.SegmentSummaryDto
import com.openrecall.relay.http.dto.SegmentsPageDto
import com.openrecall.relay.http.dto.WaveformDto

/**
 * Narrow seam over the `/segments` endpoints, for the same reason
 * [SessionApi] exists: [com.openrecall.relay.http.OpenRecallHttpClient] is
 * `final` and does real OkHttp I/O, so it cannot be faked in a host unit
 * test. The repository depends on this interface and tests supply a fake.
 *
 * Methods return wire DTOs; [SegmentRepositoryImpl] owns DTO → domain
 * mapping and error classification, so the mapping stays testable and total.
 */
interface SegmentApi {
    suspend fun listSegments(
        limit: Int,
        cursor: String?,
        query: String?,
        sessionId: String?,
    ): SegmentsPageDto

    suspend fun getSegment(id: SegmentId): SegmentDetailsDto
    suspend fun getSegmentMemory(id: SegmentId): SegmentMemoryDto
    suspend fun getSegmentWaveform(id: SegmentId): WaveformDto
    suspend fun renameSegment(id: SegmentId, title: String): SegmentSummaryDto
    suspend fun deleteSegment(id: SegmentId)

    /**
     * The streaming URL and the bearer token the player must send with it.
     * Returned together because a URL without the header is a 401, and the
     * platform player takes both at `setDataSource` time.
     */
    suspend fun audioSource(id: SegmentId): AudioSource
}

/** A playable segment URL plus the auth header the request needs. */
data class AudioSource(val url: String, val headers: Map<String, String>)
