package com.sense.relay.domain.model

import java.time.Instant

/**
 * A transcribed window from the server. `text` is the recognized
 * content; `durationMs` is the audio window that produced it. Phase
 * 2's mapper produces only this subtype; future kinds (images,
 * summaries) join the same timeline under a new CaptureEvent subtype.
 */
data class TranscriptChunk(
    override val id: String,
    override val sessionId: SessionId,
    override val seq: Int,
    override val startMs: Long,
    override val createdAt: Instant,
    val text: String,
    val durationMs: Int,
) : CaptureEvent
