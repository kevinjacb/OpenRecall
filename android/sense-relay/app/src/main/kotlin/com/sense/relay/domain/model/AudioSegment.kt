package com.sense.relay.domain.model

import java.time.Instant

/**
 * Placeholder for the audio plane. Defined now so the timeline model
 * is stable and the mapper can pass it through; no code reads or
 * writes it in Phase 2 — the server doesn't produce `kind: "audio"`
 * events yet, and no UI shows audio segments. Will be wired in the
 * audio plane phase.
 */
data class AudioSegment(
    override val id: String,
    override val sessionId: SessionId,
    override val seq: Int,
    override val startMs: Long,
    override val createdAt: Instant,
    val codec: String,
    val sampleRateHz: Int,
    val byteCount: Int,
) : CaptureEvent
