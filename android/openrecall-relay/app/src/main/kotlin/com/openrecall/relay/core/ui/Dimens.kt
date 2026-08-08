package com.openrecall.relay.core.ui

import androidx.compose.ui.unit.dp

/**
 * Spacing + sizing tokens. Components compose these rather than hand-rolling
 * dp values, so the rhythm of the app stays consistent across screens and
 * the next designer can adjust everything in one file.
 *
 * The 4dp base is Material's standard step. Don't add new in-between values
 * without a deliberate need; a tight scale is the point.
 */
object Spacing {
    val xs = 4.dp
    val sm = 8.dp
    val md = 16.dp
    val lg = 24.dp
    val xl = 32.dp
}

/**
 * Minimum touch target. M3 default is 48dp; we keep it explicit so a
 * future opt-in to the new 56dp guideline (M3 Expressive) is a one-line
 * change here rather than a search-and-replace.
 */
val TouchTarget = 48.dp
