package com.opensapien.relay.ui.design

import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.SenseTheme

/**
 * How the locket is rendered on Home and in pairing.
 *
 *  - [Connected] — steady green LED, slow breathe.
 *  - [Searching] — amber LED, fast pulse.
 *  - [Off]       — dim grey LED, no pulse.
 */
enum class DeviceVisualState { Connected, Searching, Off }

private val LedGreen = Color(0xFF3FBF7F)
private val LedAmber = Color(0xFFE8813F)
private val LedGrey = Color(0xFF8D8B88)

/**
 * The hero device illustration.
 *
 * **Placeholder.** The comp renders the locket with a live three.js scene
 * (`design/sense-device.js`) — a rotating, draggable, physically-lit 3D
 * model. Shipping that on Android means a real 3D pipeline (Filament or
 * SceneView) plus a `.glb` asset, neither of which exists in this repo yet.
 *
 * This is a hand-drawn 2D stand-in that keeps the silhouette, the two mic
 * ports, the engraved emblem, the side button and — most importantly — the
 * *status LED behaviour*, which is the only part of the illustration that
 * carries information. It animates: the body drifts, the LED breathes at a
 * per-state rate, and a soft halo tracks the LED. Swapping in the real model
 * later is a change to this one composable.
 */
@Composable
fun DeviceVisual(
    state: DeviceVisualState,
    modifier: Modifier = Modifier,
    height: Dp = 260.dp,
) {
    val colors = SenseTheme.colors
    val transition = rememberInfiniteTransition(label = "device")

    val ledColor = when (state) {
        DeviceVisualState.Connected -> LedGreen
        DeviceVisualState.Searching -> LedAmber
        DeviceVisualState.Off -> LedGrey
    }
    // The comp pulses the LED at ~1.7rad/s when connected and ~6rad/s while
    // scanning; off is a dim constant.
    val pulsePeriodMs = when (state) {
        DeviceVisualState.Connected -> 1800
        DeviceVisualState.Searching -> 520
        DeviceVisualState.Off -> 0
    }
    val pulse = if (pulsePeriodMs == 0) {
        0.08f
    } else {
        transition.animateFloat(
            initialValue = if (state == DeviceVisualState.Searching) 0.35f else 0.6f,
            targetValue = 1f,
            animationSpec = infiniteRepeatable(
                animation = tween(pulsePeriodMs),
                repeatMode = RepeatMode.Reverse,
            ),
            label = "led",
        ).value
    }
    // Slow vertical drift so the object feels suspended rather than pasted on.
    val drift = transition.animateFloat(
        initialValue = -1f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(3000),
            repeatMode = RepeatMode.Reverse,
        ),
        label = "drift",
    ).value

    Box(
        modifier = modifier.fillMaxWidth().height(height),
        contentAlignment = Alignment.Center,
    ) {
        Canvas(Modifier.fillMaxWidth().height(height)) {
            drawGlowBackdrop(colors.card)
            drawLocket(
                bodyColor = if (colors.isDark) Color(0xFF2B2B2E) else Color(0xFFF7F5F1),
                etchColor = if (colors.isDark) Color(0xFF56534E) else Color(0xFFA39D94),
                portColor = if (colors.isDark) Color(0xFF141416) else Color(0xFF232326),
                shadowColor = colors.ink.copy(alpha = if (colors.isDark) 0.35f else 0.12f),
                ledColor = ledColor,
                pulse = pulse,
                driftPx = drift * 6f,
            )
        }
    }
}

/** The radial wash behind the device that lifts it off the canvas. */
private fun DrawScope.drawGlowBackdrop(highlight: Color) {
    drawCircle(
        brush = Brush.radialGradient(
            colors = listOf(highlight, highlight.copy(alpha = 0f)),
            center = Offset(size.width / 2f, size.height * 0.44f),
            radius = size.minDimension * 0.62f,
        ),
        radius = size.minDimension * 0.62f,
        center = Offset(size.width / 2f, size.height * 0.44f),
    )
}

private fun DrawScope.drawLocket(
    bodyColor: Color,
    etchColor: Color,
    portColor: Color,
    shadowColor: Color,
    ledColor: Color,
    pulse: Float,
    driftPx: Float,
) {
    // Body geometry mirrors the comp's extruded shape: 1.62 x 2.24 units with
    // a 0.44 corner radius, scaled to the available height.
    val bodyH = size.height * 0.62f
    val bodyW = bodyH * (1.62f / 2.24f)
    val cx = size.width / 2f
    val cy = size.height * 0.46f + driftPx
    val left = cx - bodyW / 2f
    val top = cy - bodyH / 2f
    val corner = CornerRadius(bodyW * 0.27f, bodyW * 0.27f)

    // Contact shadow, tied to the drift so the object reads as floating.
    drawOval(
        color = shadowColor.copy(alpha = shadowColor.alpha * (1f - driftPx / 24f)),
        topLeft = Offset(cx - bodyW * 0.42f, cy + bodyH * 0.54f),
        size = Size(bodyW * 0.84f, bodyH * 0.07f),
    )

    // Body with a soft top-lit gradient.
    drawRoundRect(
        brush = Brush.verticalGradient(
            colors = listOf(bodyColor, bodyColor.copy(alpha = 0.86f)),
            startY = top,
            endY = top + bodyH,
        ),
        topLeft = Offset(left, top),
        size = Size(bodyW, bodyH),
        cornerRadius = corner,
    )
    drawRoundRect(
        color = etchColor.copy(alpha = 0.35f),
        topLeft = Offset(left, top),
        size = Size(bodyW, bodyH),
        cornerRadius = corner,
        style = Stroke(width = 1.dp.toPx()),
    )

    // Side button on the right edge.
    drawRoundRect(
        color = bodyColor,
        topLeft = Offset(left + bodyW - 1f, cy - bodyH * 0.24f),
        size = Size(bodyW * 0.045f, bodyH * 0.08f),
        cornerRadius = CornerRadius(bodyW * 0.02f, bodyW * 0.02f),
    )

    // Two mic ports near the top, each in an etched ring.
    val micR = bodyW * 0.026f
    val micY = top + bodyH * 0.16f
    listOf(-0.123f, 0.123f).forEach { dx ->
        val mx = cx + bodyW * dx
        drawCircle(color = etchColor, radius = micR * 1.5f, center = Offset(mx, micY), style = Stroke(micR * 0.4f))
        drawCircle(color = portColor, radius = micR, center = Offset(mx, micY))
    }

    // Engraved emblem: two concentric rings and a filled dot.
    val emblemY = cy
    drawCircle(color = etchColor, radius = bodyW * 0.123f, center = Offset(cx, emblemY), style = Stroke(bodyW * 0.021f))
    drawCircle(color = etchColor, radius = bodyW * 0.065f, center = Offset(cx, emblemY), style = Stroke(bodyW * 0.018f))
    drawCircle(color = etchColor, radius = bodyW * 0.021f, center = Offset(cx, emblemY))

    // Status LED with its halo — the informative part of the illustration.
    val ledY = top + bodyH * 0.855f
    val ledR = bodyW * 0.038f
    drawCircle(
        brush = Brush.radialGradient(
            colors = listOf(ledColor.copy(alpha = 0.05f + pulse * 0.32f), ledColor.copy(alpha = 0f)),
            center = Offset(cx, ledY),
            radius = ledR * (5f + pulse * 3f),
        ),
        radius = ledR * (5f + pulse * 3f),
        center = Offset(cx, ledY),
    )
    drawCircle(color = etchColor, radius = ledR * 1.45f, center = Offset(cx, ledY), style = Stroke(ledR * 0.32f))
    drawCircle(color = ledColor.copy(alpha = 0.5f + pulse * 0.5f), radius = ledR, center = Offset(cx, ledY))

    // USB-C port on the bottom edge.
    drawRoundRect(
        color = portColor,
        topLeft = Offset(cx - bodyW * 0.105f, top + bodyH - 1f),
        size = Size(bodyW * 0.21f, bodyH * 0.017f),
        cornerRadius = CornerRadius(bodyH * 0.01f, bodyH * 0.01f),
    )
}
