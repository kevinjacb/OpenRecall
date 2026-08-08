package com.opensapien.relay.ui.design

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp

/**
 * Three monochrome tones. `Accent` is reserved for the live/recording
 * state per the spec; don't use it for benign labels or the brand fades
 * into the wallpaper.
 */
enum class Tone { Neutral, Accent, Muted }

/**
 * A small monochrome status pill: "Live", "Idle", "Reconnecting", etc.
 * The styling is tone-driven, not color-driven, so adding a new tone is
 * one place to change.
 */
data class StateStyle(val label: String, val tone: Tone)

@Composable
fun StatePill(style: StateStyle, modifier: Modifier = Modifier) {
    val cs = MaterialTheme.colorScheme
    val (bg, fg) = when (style.tone) {
        Tone.Neutral -> cs.surface to cs.onSurface
        Tone.Muted -> Color.Transparent to cs.onSurfaceVariant
        Tone.Accent -> cs.primary to cs.onPrimary
    }
    Text(
        text = style.label,
        style = MaterialTheme.typography.labelMedium,
        color = fg,
        modifier = modifier
            .clip(RoundedCornerShape(50))
            .background(bg)
            .padding(horizontal = 10.dp, vertical = 4.dp),
    )
}
