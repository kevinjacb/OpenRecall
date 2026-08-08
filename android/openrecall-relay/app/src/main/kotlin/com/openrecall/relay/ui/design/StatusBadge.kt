package com.openrecall.relay.ui.design

import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.openrecall.relay.core.ui.PillShape
import com.openrecall.relay.core.ui.RecallColors
import com.openrecall.relay.core.ui.RecallTheme

/**
 * The semantic weight of a status. Drives both the pill colours and whether
 * the leading dot breathes.
 *
 *  - [Healthy] — green. Connected, listening, authenticated.
 *  - [Working] — accent. Scanning, connecting, retrying. The dot pulses.
 *  - [Idle]    — grey. Not connected, unknown, off.
 *  - [Problem] — danger. Failed, unreachable.
 */
enum class StatusTone { Healthy, Working, Idle, Problem }

private data class PillColors(val fill: Color, val label: Color, val dot: Color)

private fun StatusTone.colors(c: RecallColors): PillColors = when (this) {
    StatusTone.Healthy -> PillColors(c.okSoft, c.okInk, c.ok)
    StatusTone.Working -> PillColors(c.accentSoft, c.accentInk, c.accent)
    StatusTone.Idle -> PillColors(c.canvasSunken, c.slate, c.greyFaint)
    StatusTone.Problem -> PillColors(c.accentSoft, c.danger, c.danger)
}

/**
 * The status pill from the comp: a filled, fully-rounded lozenge with a
 * leading state dot. On [StatusTone.Working] the dot breathes, which is the
 * only motion on an otherwise still screen — it reads as "the app is doing
 * something" without a spinner.
 */
@Composable
fun StatusBadge(
    label: String,
    tone: StatusTone,
    modifier: Modifier = Modifier,
) {
    val pill = tone.colors(RecallTheme.colors)
    Row(
        modifier = modifier
            .clip(PillShape)
            .background(pill.fill)
            .padding(horizontal = 13.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(7.dp),
    ) {
        StatusDot(color = pill.dot, pulsing = tone == StatusTone.Working)
        Text(label, style = MaterialTheme.typography.labelMedium, color = pill.label)
    }
}

/**
 * A bare state dot. Extracted so list rows and card headers can show the
 * same indicator without the pill chrome.
 */
@Composable
fun StatusDot(
    color: Color,
    modifier: Modifier = Modifier,
    size: androidx.compose.ui.unit.Dp = 6.dp,
    pulsing: Boolean = false,
) {
    val alpha = if (pulsing) {
        val transition = rememberInfiniteTransition(label = "status-dot")
        transition.animateFloat(
            initialValue = 0.35f,
            targetValue = 1f,
            animationSpec = infiniteRepeatable(
                animation = tween(durationMillis = 900),
                repeatMode = RepeatMode.Reverse,
            ),
            label = "status-dot-alpha",
        ).value
    } else {
        1f
    }
    Box(
        modifier
            .size(size)
            .alpha(alpha)
            .clip(CircleShape)
            .background(color),
    )
}
