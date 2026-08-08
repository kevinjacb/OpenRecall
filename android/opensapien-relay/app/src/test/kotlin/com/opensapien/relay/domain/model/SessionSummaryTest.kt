package com.opensapien.relay.domain.model

import java.time.Instant
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * Pure-data tests for [SessionSummary]: equality, copy, nullable endedAt.
 * The shape is what every list and detail screen depends on; if it
 * changes, this test catches the change at the source.
 */
class SessionSummaryTest {

    private val t0 = Instant.parse("2026-07-04T10:00:00Z")

    private fun summary(
        id: String = "s1",
        startedAt: Instant = t0,
        endedAt: Instant? = t0.plusSeconds(60),
        durationMs: Long = 60_000,
        transcriptCount: Int = 3,
        preview: String = "hello world",
    ) = SessionSummary(
        id = SessionId(id),
        startedAt = startedAt,
        endedAt = endedAt,
        durationMs = durationMs,
        transcriptCount = transcriptCount,
        preview = preview,
    )

    @Test fun carriesAllFields() {
        val s = summary()
        assertEquals("s1", s.id.value)
        assertEquals(t0, s.startedAt)
        assertEquals(t0.plusSeconds(60), s.endedAt)
        assertEquals(60_000, s.durationMs)
        assertEquals(3, s.transcriptCount)
        assertEquals("hello world", s.preview)
    }

    @Test fun equalityIsStructural() {
        assertEquals(summary(), summary())
        assertNotEquals(summary(id = "a"), summary(id = "b"))
        assertNotEquals(summary(preview = "a"), summary(preview = "b"))
    }

    @Test fun copySemantics() {
        val s = summary()
        val s2 = s.copy(durationMs = 0, preview = "")
        assertEquals(0, s2.durationMs)
        assertEquals("", s2.preview)
        assertEquals(s.id, s2.id)
        assertEquals(s.startedAt, s2.startedAt)
    }

    @Test fun endedAtIsNullable() {
        // An in-flight session has no endedAt yet; the model must permit null.
        val live = summary(endedAt = null)
        assertNull(live.endedAt)
    }

    @Test fun seanhashAndToStringAreStable() {
        // The data class hashCode/equals contract is the only contract
        // Map<SessionSummary, ...> / .distinct() depend on.
        val a = summary()
        val b = summary()
        assertEquals(a.hashCode(), b.hashCode())
        assertTrue(a.toString().contains("s1"))
    }
}
