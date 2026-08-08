package com.openrecall.relay.ui.design

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier

/**
 * A connection status pill. The dot now lives inside [StatusBadge] (the comp
 * draws it as part of the lozenge, not beside it), so this is a thin
 * [Tone] → [StatusTone] adapter for the Device screen and other older call
 * sites.
 */
@Composable
fun ConnectionBadge(
    state: String,
    tone: Tone = Tone.Neutral,
    modifier: Modifier = Modifier,
) {
    StatusBadge(label = state, tone = tone.toStatusTone(), modifier = modifier)
}
