package com.opensapien.relay.ui.design

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier

/**
 * Legacy three-tone vocabulary, kept because the Commands, Device and
 * Timeline surfaces speak it. It maps onto the design system's richer
 * [StatusTone]; new code should use [StatusBadge] with a [StatusTone]
 * directly.
 */
enum class Tone { Neutral, Accent, Muted }

/** Label + tone, the argument shape [StatePill] takes. */
data class StateStyle(val label: String, val tone: Tone)

internal fun Tone.toStatusTone(): StatusTone = when (this) {
    Tone.Accent -> StatusTone.Working
    Tone.Neutral -> StatusTone.Healthy
    Tone.Muted -> StatusTone.Idle
}

/**
 * A small status pill. A thin adapter over [StatusBadge] so the older
 * [Tone]-based call sites render in the redesigned language without each
 * needing to be rewritten.
 */
@Composable
fun StatePill(style: StateStyle, modifier: Modifier = Modifier) {
    StatusBadge(label = style.label, tone = style.tone.toStatusTone(), modifier = modifier)
}
