package com.sense.relay.ui.design

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp

/**
 * A "dot" that sits next to a [StatePill] in a [ConnectionBadge]. The
 * live/recording state is encoded by color; the brief says animation
 * arrives in a later phase. Today the dot is a plain filled circle in
 * the active color.
 *
 * Kept as its own composable (not inlined into [ConnectionBadge]) so a
 * future screen — e.g. a list row that shows "online/offline" — can
 * use it standalone.
 */
@Composable
fun AnimatedConnectionDot(
    color: Color,
    modifier: Modifier = Modifier,
) {
    Box(
        modifier = modifier
            .size(8.dp)
            .clip(CircleShape)
            .background(color),
    )
}
