package com.opensapien.relay.ui.design

import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.keyframes
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.SenseTheme
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.sin

/**
 * The four-bar equaliser that marks the "Hearing now" card as live. Each bar
 * runs the same scale animation offset by 180ms, matching the comp's
 * staggered `bar` keyframes.
 */
@Composable
fun LiveBars(
    modifier: Modifier = Modifier,
    color: Color = SenseTheme.colors.accent,
    barHeight: Dp = 14.dp,
    bars: Int = 4,
) {
    val transition = rememberInfiniteTransition(label = "live-bars")
    Row(
        modifier = modifier.height(barHeight),
        verticalAlignment = Alignment.Bottom,
        horizontalArrangement = Arrangement.spacedBy(2.dp),
    ) {
        repeat(bars) { index ->
            val scale = transition.animateFloat(
                initialValue = 0.22f,
                targetValue = 0.22f,
                animationSpec = infiniteRepeatable(
                    animation = keyframes {
                        durationMillis = 1100
                        0.22f at 0
                        1f at 550
                        0.22f at 1100
                    },
                    repeatMode = RepeatMode.Restart,
                    initialStartOffset = androidx.compose.animation.core.StartOffset(index * 180),
                ),
                label = "bar-$index",
            ).value
            Box(
                Modifier
                    .width(2.5.dp)
                    .fillMaxHeight()
                    .scale(scaleX = 1f, scaleY = scale)
                    .clip(RoundedCornerShape(2.dp))
                    .background(color),
            )
        }
    }
}

/**
 * A static waveform strip for the session player.
 *
 * **Placeholder.** The server exposes no per-session audio waveform (or
 * audio download) endpoint, so the bar heights are derived deterministically
 * from the session id rather than from real samples — the same shape every
 * time you open a given session, but not that session's actual audio. It is
 * here so the player's layout is real; wire it to sample data when the
 * endpoint lands.
 */
@Composable
fun WaveformStrip(
    seed: String,
    modifier: Modifier = Modifier,
    bars: Int = 34,
    height: Dp = 36.dp,
    color: Color = SenseTheme.colors.track,
) {
    val offset = seed.hashCode().toDouble()
    Row(
        modifier = modifier.fillMaxWidth().height(height),
        verticalAlignment = Alignment.Bottom,
        horizontalArrangement = Arrangement.spacedBy(2.dp),
    ) {
        repeat(bars) { i ->
            // Mirrors the comp's `8 + |sin(i*1.7) * cos(i*0.6)| * 28` curve,
            // phase-shifted by the seed so different sessions differ.
            val t = i + offset % 7.0
            val fraction = (0.22f + abs(sin(t * 1.7) * cos(t * 0.6)).toFloat() * 0.78f)
            Box(
                Modifier
                    .weight(1f)
                    .fillMaxHeight(fraction)
                    .clip(RoundedCornerShape(2.dp))
                    .background(color),
            )
        }
    }
}
