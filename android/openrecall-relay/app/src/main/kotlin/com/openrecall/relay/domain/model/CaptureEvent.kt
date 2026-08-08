package com.openrecall.relay.domain.model

import java.time.Instant

/**
 * A single event in a session's timeline. The session-scoped shared
 * fields (id, sessionId, seq, startMs, createdAt) are the columns the
 * UI needs to render any kind — a transcript chunk and an audio segment
 * both go in the same vertical timeline, ordered by `seq`.
 *
 * `id` is the server's stable per-event id (the gateway's
 * ULID). `sessionId` is denormalized so consumers can route without a
 * join. `seq` is the contiguous index inside the session (used for
 * the server's `request_chunks` backfill). `startMs` is the elapsed
 * milliseconds from session start. `createdAt` is wall-clock for
 * cross-session sorts.
 *
 * The hierarchy is sealed so a `when` over a `CaptureEvent` stays
 * exhaustive — add a new subtype only if a new capture plane exists.
 */
sealed interface CaptureEvent {
    val id: String
    val sessionId: SessionId
    val seq: Int
    val startMs: Long
    val createdAt: Instant
}
