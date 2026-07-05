package com.sense.relay.domain.model

import java.time.Instant

/**
 * Lightweight per-session metadata used by the recordings list and the
 * dashboard's "last session" tile. `endedAt` is null while the session
 * is still in flight (the gateway hasn't sent a Bye yet). `durationMs`
 * is the server's view of session length, not a UI recompute.
 */
data class SessionSummary(
    val id: SessionId,
    val startedAt: Instant,
    val endedAt: Instant?,
    val durationMs: Long,
    val transcriptCount: Int,
    val preview: String,
)
