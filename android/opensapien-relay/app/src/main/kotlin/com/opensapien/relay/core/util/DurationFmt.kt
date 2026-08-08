package com.opensapien.relay.core.util

import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.roundToLong

/**
 * "HH:MM:SS" formatter for a duration in milliseconds. We use 24h+ format
 * (no days rollover) because the longest single session in the product is
 * hours, not days, and rolling over would lose information at the glance.
 *
 * Sub-second ms are floored. Negative inputs are treated as 0 (defensive
 * against clock skew).
 */
fun formatHmMs(ms: Long): String {
    val safe = if (ms < 0) 0L else ms
    val totalSeconds = safe / 1000
    val h = totalSeconds / 3600
    val m = (totalSeconds % 3600) / 60
    val s = totalSeconds % 60
    return "%02d:%02d:%02d".format(h, m, s)
}

/**
 * Human-friendly relative-time formatter. Buckets are:
 *   < 1 min   -> "just now"
 *   < 1 h     -> "N min ago"  (rounded to nearest minute)
 *   < 24 h    -> "N h ago"    (rounded to nearest hour)
 *   < 48 h    -> "yesterday"
 *   otherwise -> "MMM d"       (e.g. "Mar 14") in the device locale
 *
 * `nowMs` and `thenMs` are milliseconds since epoch; pass a [Clock.nowMs] in
 * production for testability.
 */
fun formatRelative(nowMs: Long, thenMs: Long): String {
    val diffMs = (nowMs - thenMs).coerceAtLeast(0L)
    val minuteMs = 60_000L
    val hourMs = 60 * minuteMs
    val dayMs = 24 * hourMs

    return when {
        diffMs < minuteMs -> "just now"
        diffMs < hourMs -> {
            // Round to nearest minute. 90s -> 2, 89s -> 1, 30s -> 1, 31s -> 1.
            // (The "N min ago" label is the user's mental model; rounding to
            // nearest matches the way humans say "about a minute ago".)
            val minutes = (diffMs.toDouble() / minuteMs).roundToLong().coerceAtLeast(1L)
            "$minutes min ago"
        }
        diffMs < dayMs -> {
            val hours = (diffMs.toDouble() / hourMs).roundToLong().coerceAtLeast(1L)
            "$hours h ago"
        }
        diffMs < 2 * dayMs -> "yesterday"
        else -> {
            // Locale-aware short month + day. SimpleDateFormat is fine for
            // a small fixed set of patterns; java.time is a larger refactor.
            val sdf = SimpleDateFormat("MMM d", Locale.getDefault())
            sdf.format(Date(thenMs))
        }
    }
}
