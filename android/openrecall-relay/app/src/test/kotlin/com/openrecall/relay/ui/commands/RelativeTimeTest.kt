package com.openrecall.relay.ui.commands

import org.junit.Assert.assertEquals
import org.junit.Test
import java.time.Instant

class RelativeTimeTest {

    private val now: Instant = Instant.parse("2026-07-15T12:00:00Z")

    @Test
    fun `0 seconds ago is just now`() {
        assertEquals("just now", relativeTime(now, now))
    }

    @Test
    fun `1 second ago is 1s ago`() {
        assertEquals("1s ago", relativeTime(now.minusSeconds(1), now))
    }

    @Test
    fun `59 seconds ago is 59s ago`() {
        assertEquals("59s ago", relativeTime(now.minusSeconds(59), now))
    }

    @Test
    fun `60 seconds ago is 1m ago`() {
        assertEquals("1m ago", relativeTime(now.minusSeconds(60), now))
    }

    @Test
    fun `119 seconds ago is 1m ago`() {
        assertEquals("1m ago", relativeTime(now.minusSeconds(119), now))
    }

    @Test
    fun `120 seconds ago is 2m ago`() {
        assertEquals("2m ago", relativeTime(now.minusSeconds(120), now))
    }

    @Test
    fun `60 minutes ago is 1h ago`() {
        assertEquals("1h ago", relativeTime(now.minusSeconds(60 * 60), now))
    }

    @Test
    fun `24 hours ago is 1d ago`() {
        assertEquals("1d ago", relativeTime(now.minusSeconds(24 * 60 * 60), now))
    }

    @Test
    fun `48 hours ago is 2d ago`() {
        assertEquals("2d ago", relativeTime(now.minusSeconds(48 * 60 * 60), now))
    }

    @Test
    fun `30 days ago is ISO date`() {
        val then = now.minusSeconds(30L * 24 * 60 * 60)
        val expected = "2026-06-15" // 2026-07-15 minus 30 days
        assertEquals(expected, relativeTime(then, now))
    }

    @Test
    fun `negative diff clamps to just now`() {
        // then is AFTER now (e.g. clock skew)
        val future = now.plusSeconds(120)
        assertEquals("just now", relativeTime(future, now))
    }
}
