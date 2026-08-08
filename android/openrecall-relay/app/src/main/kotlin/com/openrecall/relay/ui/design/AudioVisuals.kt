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
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.rememberUpdatedState
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
 * in the track colour, plus a playhead you can drag like a music player.
 *
 * [peaks] are amplitudes in `0f..1f`, already resampled to the bar count the
 * caller wants — the server publishes fixed 500 ms buckets precisely so it
 * doesn't have to know this display constant.
 *
 * Bars are laid out with fixed weights, so bar `i` covers a known fraction of
 * the width and the fill boundary lines up with the playhead exactly.
 *
 * Gestures are one pointer stream rather than a tap detector plus a drag
 * detector: a single `awaitEachGesture` loop means the touch-down already
 * reports a position (so a tap seeks), every move reports the new one (so a
 * drag runs forwards *and* backwards), and the release reports the final one.
 * The caller decides what each means — the convention here is that [onScrub]
 * previews (move the playhead, don't touch the decoder) and [onScrubEnd]
 * commits the seek, which keeps a drag from firing dozens of `seekTo` calls.
 *
 * Passing a null [onScrub] leaves the strip inert — no gestures, no playhead.
 *
 * The gesture handler is installed unconditionally and re-reads its callbacks
 * through [rememberUpdatedState] on every event. Both details matter:
 * `pointerInput(Unit)` never restarts its block, so a chain that only gains the
 * modifier once the stream is ready would keep whatever lambdas existed when
 * the node was built — capturing a duration of zero and silently swallowing
 * every seek. Reading the latest lambda per event sidesteps that entirely.
 */
@Composable
fun WaveformScrubber(
    peaks: List<Float>,
    progress: Float,
    modifier: Modifier = Modifier,
    height: Dp = 36.dp,
    onScrub: ((Float) -> Unit)? = null,
    onScrubEnd: ((Float) -> Unit)? = null,
    playedColor: Color = RecallTheme.colors.accent,
    remainingColor: Color = RecallTheme.colors.track,
) {
    val clamped = progress.coerceIn(0f, 1f)
    val currentScrub by rememberUpdatedState(onScrub)
    val currentScrubEnd by rememberUpdatedState(onScrubEnd)
    Box(
        modifier = modifier
            .fillMaxWidth()
            .height(height)
            .pointerInput(Unit) {
                awaitEachGesture {
                    val down = awaitFirstDown(requireUnconsumed = false)
                    val width = size.width.toFloat()
                    val scrub = currentScrub
                    if (width <= 0f || scrub == null) return@awaitEachGesture
                    // Consuming from the down onwards is what stops an enclosing
                    // scrollable from reading the drag as a scroll and stealing it.
                    down.consume()
                    var fraction = (down.position.x / width).coerceIn(0f, 1f)
                    scrub(fraction)
                    while (true) {
                        val change = awaitPointerEvent().changes
                            .firstOrNull { it.id == down.id } ?: break
                        if (!change.pressed) break
                        fraction = (change.position.x / width).coerceIn(0f, 1f)
                        currentScrub?.invoke(fraction)
                        change.consume()
                    }
                    currentScrubEnd?.invoke(fraction)
                }
            },
    ) {
        Row(
            modifier = Modifier.fillMaxSize(),
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
        if (onScrub != null) {
            // The playhead: a full-height rule at the progress boundary, so the
            // strip reads as draggable and the drag has something to follow.
            // Width is never quite 0 — at fraction 0 the rule would otherwise
            // have nothing to end-align against.
            Box(
                Modifier
                    .fillMaxHeight()
                    .fillMaxWidth(clamped.coerceIn(0.012f, 1f)),
                contentAlignment = Alignment.CenterEnd,
            ) {
                Box(
                    Modifier
                        .width(3.dp)
                        .fillMaxHeight()
                        .clip(RoundedCornerShape(2.dp))
                        .background(playedColor),
                )
            }
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
    Row(
        modifier = modifier.fillMaxWidth().height(height),
        verticalAlignment = Alignment.Bottom,
        horizontalArrangement = Arrangement.spacedBy(2.dp),
    ) {
        placeholderPeaks(seed, bars).forEach { peak ->
            Box(
                Modifier
                    .weight(1f)
                    .fillMaxHeight(peak)
                    .clip(RoundedCornerShape(2.dp))
                    .background(color),
            )
        }
    }
}

/**
 * The placeholder bar curve, as `0f..1f` amplitudes.
 *
 * Mirrors the comp's `8 + |sin(i*1.7) * cos(i*0.6)| * 28` shape, phase-shifted
 * by [seed] so different recordings differ. Shared with [WaveformScrubber] so
 * that a recording whose audio plays but whose peaks the server never
 * published still gets a strip you can drag, instead of an inert decoration.
 */
fun placeholderPeaks(seed: String, bars: Int = 34): List<Float> {
    val offset = seed.hashCode().toDouble() % 7.0
    return (0 until bars).map { i ->
        val t = i + offset
        (0.22f + abs(sin(t * 1.7) * cos(t * 0.6)).toFloat() * 0.78f).coerceIn(0f, 1f)
    }
}
