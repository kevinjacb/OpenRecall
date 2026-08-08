package com.openrecall.relay.ui.recordings

import com.openrecall.relay.domain.model.SessionId
import com.openrecall.relay.domain.model.SessionSummary
import java.time.Instant
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * The Recordings list groups sessions into Today / Yesterday / Earlier, as
 * the `design/` comp does. The server sends no grouping of its own, so the
 * buckets are derived here — and derived wrong is a silently misfiled
 * recording, hence these tests.
 */
class SessionGroupingTest {

    private val now = Instant.parse("2026-08-08T12:00:00Z").toEpochMilli()
    private val hour = 60 * 60 * 1000L
    private val day = 24 * hour

    private fun session(id: String, startedAtMs: Long) = SessionSummary(
        id = SessionId(id),
        startedAt = Instant.ofEpochMilli(startedAtMs),
        endedAt = null,
        durationMs = 1_000,
        transcriptCount = 1,
        preview = "hello",
    )

    @Test
    fun `buckets by age relative to now`() {
        val groups = groupByRecency(
            listOf(
                session("a", now - hour),          // today
                session("b", now - 30 * hour),     // yesterday
                session("c", now - 5 * day),       // earlier
            ),
            nowMs = now,
        )
        assertEquals(listOf("Today", "Yesterday", "Earlier"), groups.map { it.label })
        assertEquals(listOf("a"), groups[0].items.map { it.id.value })
        assertEquals(listOf("b"), groups[1].items.map { it.id.value })
        assertEquals(listOf("c"), groups[2].items.map { it.id.value })
    }

    @Test
    fun `empty buckets are dropped rather than rendered as empty headings`() {
        val groups = groupByRecency(listOf(session("a", now - hour)), nowMs = now)
        assertEquals(listOf("Today"), groups.map { it.label })
    }

    @Test
    fun `boundaries fall on the later bucket`() {
        // Exactly 24h old is no longer "today"; exactly 48h is no longer
        // "yesterday". Off-by-one here misfiles a recording at midnight.
        val groups = groupByRecency(
            listOf(session("a", now - day), session("b", now - 2 * day)),
            nowMs = now,
        )
        assertEquals(listOf("Yesterday", "Earlier"), groups.map { it.label })
    }

    @Test
    fun `ordering within a bucket is preserved`() {
        // The server returns newest-first; grouping must not reshuffle.
        val groups = groupByRecency(
            listOf(session("a", now - hour), session("b", now - 2 * hour)),
            nowMs = now,
        )
        assertEquals(listOf("a", "b"), groups.single().items.map { it.id.value })
    }

    @Test
    fun `no sessions means no groups`() {
        assertTrue(groupByRecency(emptyList(), nowMs = now).isEmpty())
    }
}
