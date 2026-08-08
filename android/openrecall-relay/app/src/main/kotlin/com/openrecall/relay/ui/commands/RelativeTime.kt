package com.openrecall.relay.ui.commands

import java.time.Instant
import java.time.LocalDate
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter

/**
 * "2s ago" / "1m ago" / "3h ago" / "2d ago" for a [then] instant
 * relative to [now]. English-only (no localization in this
 * change, matching the rest of the app).
 *
 * - diff < 0 (future timestamp) -> "just now" (clock-skew safe)
 * - diff in [0, 60)s -> "Ns ago" (0 -> "just now")
 * - diff in [60s, 60m) -> "Nm ago"
 * - diff in [60m, 24h) -> "Nh ago"
 * - diff in [24h, 30d) -> "Nd ago"
 * - diff >= 30d -> ISO date (yyyy-MM-dd) at the [then] zone
 *
 * Pure function; no Android dependencies; testable on the host JVM.
 */
fun relativeTime(then: Instant, now: Instant): String {
    val diffSec = (now.epochSecond - then.epochSecond).coerceAtLeast(0L)
    return when {
        diffSec < 60L -> if (diffSec == 0L) "just now" else "${diffSec}s ago"
        diffSec < 60L * 60 -> "${diffSec / 60L}m ago"
        diffSec < 24L * 60L * 60 -> "${diffSec / (60L * 60L)}h ago"
        diffSec < 30L * 24L * 60L * 60 -> "${diffSec / (24L * 60L * 60L)}d ago"
        else -> DateTimeFormatter.ISO_LOCAL_DATE.format(
            LocalDate.ofInstant(then, ZoneOffset.UTC),
        )
    }
}
