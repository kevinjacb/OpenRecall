package com.sense.relay.domain.model

/**
 * The bundle the SessionDetail screen renders: the summary header plus
 * the per-session event timeline. Fetched together by
 * `GET /sessions/{id}` (Phase 3); Phase 2's fake repository
 * composes the two from in-memory data.
 */
data class SessionDetails(
    val summary: SessionSummary,
    val events: List<CaptureEvent>,
)
