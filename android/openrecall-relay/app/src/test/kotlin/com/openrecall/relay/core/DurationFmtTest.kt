package com.openrecall.relay.core

import com.openrecall.relay.core.util.formatHmMs
import com.openrecall.relay.core.util.formatRelative
import kotlin.test.Test
import kotlin.test.assertEquals

/**
 * Duration formatting is a UI contract: the labels rendered on cards and
 * detail screens. Once these strings ship, they become part of the product's
 * visual language — tests pin the exact format so accidental drift surfaces
 * in CI, not in screenshots.
 */
class DurationFmtTest {

    // --- formatHmMs: "HH:MM:SS" ---

    @Test fun hmsZero() {
        assertEquals("00:00:00", formatHmMs(0L))
    }

    @Test fun hmsOneHourOneMinuteOneSecond() {
        // 1h1m1s = 3,661,000 ms
        assertEquals("01:01:01", formatHmMs(3_661_000L))
    }

    @Test fun hmsFloorsSubSecond() {
        // Sub-second ms are dropped, never rounded up.
        assertEquals("00:00:00", formatHmMs(999L))
    }

    @Test fun hmsLongerThanDay() {
        // 25h = 90,000 seconds = 90,000,000 ms. We don't gate by 24h.
        assertEquals("25:00:00", formatHmMs(90_000_000L))
    }

    @Test fun hmsNegativeIsTreatedAsZero() {
        // Defensive: a clock skew that yields a negative duration shouldn't crash.
        assertEquals("00:00:00", formatHmMs(-1L))
    }

    // --- formatRelative: "just now" / "N min ago" / "Nh ago" / "yesterday" / "MMM d" ---

    @Test fun relativeNowIsJustNow() {
        val now = 1_700_000_000_000L
        assertEquals("just now", formatRelative(now, now))
    }

    @Test fun relativeUnderOneMinuteIsJustNow() {
        val now = 1_700_000_000_000L
        assertEquals("just now", formatRelative(now, now - 30_000L))
    }

    @Test fun relativeAtOneMinuteBoundary() {
        val now = 1_700_000_000_000L
        // 60s ago is the first "N min ago" (n=1).
        assertEquals("1 min ago", formatRelative(now, now - 60_000L))
    }

    @Test fun relativeNinetySeconds() {
        val now = 1_700_000_000_000L
        // 90s ago is the "2 min ago" case used in the brief.
        assertEquals("2 min ago", formatRelative(now, now - 90_000L))
    }

    @Test fun relativeUnderOneHourUsesMinutes() {
        val now = 1_700_000_000_000L
        assertEquals("45 min ago", formatRelative(now, now - 45 * 60_000L))
    }

    @Test fun relativeOneHourBoundary() {
        val now = 1_700_000_000_000L
        assertEquals("1 h ago", formatRelative(now, now - 60 * 60_000L))
    }

    @Test fun relativeMultiHour() {
        val now = 1_700_000_000_000L
        assertEquals("3 h ago", formatRelative(now, now - 3 * 60 * 60_000L))
    }

    @Test fun relativeYesterday() {
        // Anywhere in the previous calendar day (in the user's offset, but the
        // formatter treats "yesterday" as 24–48h ago regardless of clock hour).
        val now = 1_700_000_000_000L
        val yesterday = now - 25 * 60 * 60_000L
        assertEquals("yesterday", formatRelative(now, yesterday))
    }

    @Test fun relativeOlderThanTwoDaysUsesMonthDay() {
        val now = 1_700_000_000_000L
        // 3 days ago → "MMM d" form, e.g. "Nov 13".
        val threeDaysAgo = now - 3L * 24 * 60 * 60_000L
        // Verify the general shape: "MMM d" with a 3-letter month and a day
        // number. We don't pin the exact month (depends on `now`); we pin the
        // pattern so accidental drift surfaces.
        val out = formatRelative(now, threeDaysAgo)
        assertEquals(6, out.length, "expected 'MMM d' (6 chars), got '$out'")
        val parts = out.split(" ")
        assertEquals(2, parts.size, "expected two parts, got '$out'")
        assertEquals(3, parts[0].length, "month must be 3 letters, got '${parts[0]}'")
        assertEquals(parts[1].toIntOrNull()?.let { it in 1..31 }, true,
            "day must be a 1-31 number, got '${parts[1]}'")
    }
}
