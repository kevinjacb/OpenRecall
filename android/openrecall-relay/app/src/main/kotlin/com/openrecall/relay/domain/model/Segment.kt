package com.openrecall.relay.domain.model

import java.time.Instant

/**
 * Opaque segment identifier. The server's format is `"<session_id>:<seq>"` —
 * deterministic, so a server-side index rebuild reproduces identical ids and
 * durable metadata (titles) stays attached. Nothing outside the data layer
 * should parse it; the colon is why routes have to URL-encode it.
 */
@JvmInline
value class SegmentId(val value: String)

/**
 * One recording, as the app understands the word: a contiguous run of
 * transcript activity, closed by a server-side idle timer.
 *
 * This is the entity the Recordings list, Home's recent rows and the detail
 * screen address. A *session* is a different thing — the foreground service's
 * lifetime, spanning every reconnect for hours or days — and is not a
 * recording in any sense a user would recognise.
 *
 * [endedAt] is the last event's time even while [closed] is false; an open
 * segment is still growing, so its duration keeps moving.
 */
data class Segment(
    val id: SegmentId,
    val sessionId: SessionId,
    /** Server-side title (LLM-generated on close, or user-set). Null until
     *  the titler has run — [displayTitle] is what the UI should render. */
    val title: String?,
    val startedAt: Instant,
    val endedAt: Instant?,
    val durationMs: Long,
    val transcriptCount: Int,
    val memoryCount: Int,
    val preview: String,
    val hasAudio: Boolean,
    val closed: Boolean,
    /** The matching transcript window, on rows returned by a search. */
    val matchSnippet: String? = null,
) {
    /**
     * What to put on the row. Falls back through title → preview → a short id,
     * so a segment the titler hasn't reached yet still reads as something
     * rather than as a blank line.
     */
    val displayTitle: String
        get() = title?.takeIf { it.isNotBlank() }
            ?: preview.takeIf { it.isNotBlank() }?.let { previewTitle(it) }
            ?: "Recording ${id.value.substringAfter(':', id.value.take(8))}"
}

/** First sentence-ish of the preview, capped — a stand-in headline, not a title. */
private fun previewTitle(preview: String): String {
    val firstLine = preview.trim().lineSequence().first().trim()
    return if (firstLine.length <= 48) firstLine else firstLine.take(47).trimEnd() + "…"
}

/** A segment plus its slice of the session's event stream. */
data class SegmentDetails(
    val summary: Segment,
    val events: List<CaptureEvent>,
)

/**
 * Precomputed audio peaks for the scrubber, each in `0f..1f`.
 *
 * Bucket `i` covers `[i*bucketMs, (i+1)*bucketMs)` of **segment time**, gaps
 * included as zeroes. That invariant is what lets the playhead line up with
 * transcript `startMs` without the client decoding any audio.
 */
data class Waveform(
    val bucketMs: Int,
    val peaks: List<Float>,
    val durationMs: Long,
) {
    /**
     * Resample to exactly [bars] values for display. The server deliberately
     * doesn't know the client's bar count, so the downsample lives here.
     * Returns an empty list when there is nothing to draw.
     */
    fun resampled(bars: Int): List<Float> {
        if (peaks.isEmpty() || bars <= 0) return emptyList()
        if (peaks.size <= bars) return peaks
        val perBar = peaks.size.toDouble() / bars
        return (0 until bars).map { i ->
            val from = (i * perBar).toInt()
            val to = (((i + 1) * perBar).toInt()).coerceAtLeast(from + 1).coerceAtMost(peaks.size)
            peaks.subList(from, to).max()
        }
    }
}
