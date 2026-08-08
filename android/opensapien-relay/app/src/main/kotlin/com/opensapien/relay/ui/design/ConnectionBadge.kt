package com.opensapien.relay.ui.design

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

/**
 * Composite of [AnimatedConnectionDot] + [StatePill]. The `state` is
 * a free-form string in this slice (so a stub caller doesn't need a
 * sealed type); a later phase will replace it with a sealed
 * `ConnectionState` and pick the tone/color from the variant.
 *
 * Visual order: dot first, then a small gap, then the pill — this
 * reads as "connection indicator: status" left-to-right in LTR and
 * mirrors cleanly in RTL.
 */
@Composable
fun ConnectionBadge(
    state: String,
    tone: Tone = Tone.Neutral,
    modifier: Modifier = Modifier,
) {
    val dotColor = when (tone) {
        Tone.Neutral -> MaterialTheme.colorScheme.onSurfaceVariant
        Tone.Muted -> MaterialTheme.colorScheme.outline
        Tone.Accent -> MaterialTheme.colorScheme.primary
    }
    Row(
        modifier = modifier.padding(horizontal = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        AnimatedConnectionDot(color = dotColor)
        StatePill(style = StateStyle(label = state, tone = tone))
    }
}
