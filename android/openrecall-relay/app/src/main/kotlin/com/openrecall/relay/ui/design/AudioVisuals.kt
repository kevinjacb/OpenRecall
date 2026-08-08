package com.openrecall.relay.ui.design

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
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.openrecall.relay.core.ui.RecallTheme
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
    color: Color = RecallTheme.colors.accent,
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
 * The scrubber: real audio peaks, with the played portion filled and the rest
 * in the track colour. Tapping or dragging reports a 0..1 position through
 * [onSeek].
 *
 * [peaks] are amplitudes in `0f..1f`, already resampled to the bar count the
 * caller wants — the server publishes fixed 500 ms buckets precisely so it
 * doesn't have to know this display constant.
 *
 * Bars are laid out with fixed weights, so bar `i` covers a known fraction of
 * the width and the fill boundary lines up with the playhead exactly.
 */
@Composable
fun WaveformScrubber(
    peaks: List<Float>,
    progress: Float,
    modifier: Modifier = Modifier,
    height: Dp = 36.dp,
    onSeek: ((Float) -> Unit)? = null,
    playedColor: Color = RecallTheme.colors.accent,
    remainingColor: Color = RecallTheme.colors.track,
) {
    val clamped = progress.coerceIn(0f, 1f)
    val seekModifier = if (onSeek != null) {
        Modifier
            .pointerInput(Unit) {
                detectTapGestures { offset ->
                    if (size.width > 0) onSeek(offset.x / size.width)
                }
            }
            .pointerInput(Unit) {
                detectHorizontalDragGestures { change, _ ->
                    if (size.width > 0) onSeek(change.position.x / size.width)
                }
            }
    } else {
        Modifier
    }
    Row(
        modifier = modifier
            .fillMaxWidth()
            .height(height)
            .then(seekModifier),
        verticalAlignment = Alignment.Bottom,
        horizontalArrangement = Arrangement.spacedBy(2.dp),
    ) {
        peaks.forEachIndexed { i, peak ->
            // A zero-amplitude bucket (silence, or a gap the reassembler
            // dropped) still needs a visible baseline, or the strip reads as
            // missing data rather than as quiet.
            val fraction = (0.08f + peak.coerceIn(0f, 1f) * 0.92f)
            val played = peaks.isNotEmpty() && (i + 1f) / peaks.size <= clamped
            Box(
                Modifier
                    .weight(1f)
                    .fillMaxHeight(fraction)
                    .clip(RoundedCornerShape(2.dp))
                    .background(if (played) playedColor else remainingColor),
            )
        }
    }
}

/**
 * A static waveform strip used where no peak data exists.
 *
 * **Placeholder.** Bar heights are derived deterministically from a seed
 * rather than from samples — the same shape every time you open a given
 * recording, but not that recording's actual audio. Use [WaveformScrubber]
 * whenever the server has peaks; this is the "recording has no audio" layout.
 */
@Composable
fun WaveformStrip(
    seed: String,
    modifier: Modifier = Modifier,
    bars: Int = 34,
    height: Dp = 36.dp,
    color: Color = RecallTheme.colors.track,
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
