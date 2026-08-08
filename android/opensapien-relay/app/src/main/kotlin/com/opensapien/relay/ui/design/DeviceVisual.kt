package com.opensapien.relay.ui.design

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.DrawStyle
import androidx.compose.ui.graphics.drawscope.Fill
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.SenseTheme
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.pow
import kotlin.math.sin

/**
 * How the locket is rendered on Home and in pairing.
 *
 *  - [Connected] — steady green LED, slow breathe, slow spin.
 *  - [Searching] — amber LED, fast pulse, faster spin.
 *  - [Off]       — dim grey LED, no pulse, barely turning.
 */
enum class DeviceVisualState { Connected, Searching, Off }

private val LedGreen = Color(0xFF3FBF7F)
private val LedAmber = Color(0xFFE8813F)
private val LedGrey = Color(0xFF8D8B88)

/**
 * Idle auto-rotation, in radians per second. Connected and searching are the comp's
 * `STATES.speed`; off is lifted from the comp's 0.10 (one turn per minute, which
 * reads as a frozen image) to something that still looks alive on a phone.
 */
private fun DeviceVisualState.spinSpeed(): Float = when (this) {
    DeviceVisualState.Connected -> 0.30f
    DeviceVisualState.Searching -> 0.75f
    DeviceVisualState.Off -> 0.22f
}

/** Radians of yaw per pixel of horizontal drag — the comp uses `dx * 0.010`. */
private const val DRAG_RADIANS_PER_PX = 0.010f

/** Per-frame decay applied to fling velocity, normalised to 60fps. */
private const val SPIN_DECAY_PER_FRAME = 0.94f

/** Yaw the device rests at before the idle rotation ramps in. */
private const val START_YAW = -0.55f

/** Body depth as a fraction of body width (comp: 0.30 deep, 1.62 wide). */
private const val DEPTH_RATIO = 0.185f

/**
 * The hero device illustration.
 *
 * The comp renders the locket with a live three.js scene (`design/sense-device.js`):
 * it turns on its own, can be grabbed and spun with inertia, bobs as if suspended,
 * and pops when the connection state changes. Shipping that scene as-is means a 3D
 * pipeline (Filament or SceneView) plus a `.glb` asset, neither of which exists in
 * this repo yet.
 *
 * This is a hand-drawn 2.5D stand-in that reproduces the comp's *motion* on a
 * Compose canvas: the same per-state yaw speeds, the same drag-with-inertia feel,
 * the same 1.05rad/s float and breathing contact shadow, and the same scale pop on
 * a state change. Rotation is faked by projecting the body — the front face narrows
 * with `cos(yaw)` while the extruded side edge widens with `sin(yaw)` — so the
 * silhouette turns without a real mesh. Face detail (mics, emblem, LED) fades out as
 * the device turns edge-on; the LED halo stays faintly visible from behind so the
 * status cue never disappears entirely. Swapping in the real model later is a change
 * to this one composable.
 */
@Composable
fun DeviceVisual(
    state: DeviceVisualState,
    modifier: Modifier = Modifier,
    height: Dp = 260.dp,
) {
    val colors = SenseTheme.colors

    var yaw by remember { mutableFloatStateOf(START_YAW) }
    var spin by remember { mutableFloatStateOf(0f) }
    var idleRamp by remember { mutableFloatStateOf(0f) }
    var elapsed by remember { mutableFloatStateOf(0f) }
    var pop by remember { mutableFloatStateOf(0f) }

    LaunchedEffect(state) {
        // A state change pops the scale, exactly as the comp does on connect —
        // but not on the first frame, where there is nothing to react to.
        if (elapsed > 0f) pop = 1f
        val speed = state.spinSpeed()
        var last = 0L
        while (true) {
            withFrameNanos { now ->
                val dt = if (last == 0L) 0f else ((now - last) / 1_000_000_000f).coerceAtMost(0.05f)
                last = now
                elapsed += dt

                // The idle spin is unconditional: each drag sample knocks
                // [idleRamp] back to zero, so the finger owns the rotation while
                // it is moving and the auto-rotation fades back in about a second
                // after it stops. Nothing can latch it off — a gesture that ends
                // without an end/cancel callback (the scroll container stealing
                // the pointer, say) would otherwise freeze the device for good.
                idleRamp = (idleRamp + dt * 0.9f).coerceAtMost(1f)
                yaw += spin + speed * dt * idleRamp
                spin *= SPIN_DECAY_PER_FRAME.pow(dt * 60f)

                if (pop > 0f) pop = (pop - dt * 1.6f).coerceAtLeast(0f)
            }
        }
    }

    Box(
        modifier = modifier
            .fillMaxWidth()
            .height(height)
            .pointerInput(Unit) {
                detectHorizontalDragGestures(
                    onDragStart = { spin = 0f },
                ) { change, dragAmount ->
                    change.consume()
                    val delta = dragAmount * DRAG_RADIANS_PER_PX
                    yaw += delta
                    // Hold the idle spin off for as long as the finger keeps
                    // moving, and leave the last sample behind as fling velocity.
                    idleRamp = 0f
                    spin = delta
                }
            },
        contentAlignment = Alignment.Center,
    ) {
        Canvas(Modifier.fillMaxWidth().height(height)) {
            // The float and its shadow share one phase, so the object reads as
            // rising off the page rather than sliding on it.
            val bob = sin(elapsed * 1.05f)
            drawGlowBackdrop(colors.card)
            drawLocket(
                bodyColor = if (colors.isDark) Color(0xFF2B2B2E) else Color(0xFFF7F5F1),
                etchColor = if (colors.isDark) Color(0xFF56534E) else Color(0xFFA39D94),
                portColor = if (colors.isDark) Color(0xFF141416) else Color(0xFF232326),
                shadowColor = colors.ink.copy(alpha = if (colors.isDark) 0.35f else 0.12f),
                ledColor = state.ledColor(),
                pulse = state.pulse(elapsed),
                yaw = yaw,
                bob = bob,
                // sin(pop * PI) rises then falls, so the pop swells and settles.
                popScale = 1f + sin(pop * PI.toFloat()) * 0.075f,
            )
        }
    }
}

private fun DeviceVisualState.ledColor(): Color = when (this) {
    DeviceVisualState.Connected -> LedGreen
    DeviceVisualState.Searching -> LedAmber
    DeviceVisualState.Off -> LedGrey
}

/**
 * LED brightness in `0..1`. The comp breathes at ~1.7rad/s when connected, strobes
 * at ~6rad/s while scanning, and holds a dim constant when off.
 */
private fun DeviceVisualState.pulse(t: Float): Float = when (this) {
    DeviceVisualState.Connected -> 0.62f + 0.38f * (0.5f + 0.5f * sin(t * 1.7f))
    DeviceVisualState.Searching -> 0.35f + 0.65f * (0.5f + 0.5f * sin(t * 6.0f))
    DeviceVisualState.Off -> 0.08f
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

/** An axis-aligned ellipse — a circle squashed by the yaw projection. */
private fun DrawScope.drawEllipse(
    color: Color,
    center: Offset,
    radiusX: Float,
    radiusY: Float,
    style: DrawStyle = Fill,
) {
    if (radiusX <= 0f || radiusY <= 0f) return
    drawOval(
        color = color,
        topLeft = Offset(center.x - radiusX, center.y - radiusY),
        size = Size(radiusX * 2f, radiusY * 2f),
        style = style,
    )
}

private fun DrawScope.drawLocket(
    bodyColor: Color,
    etchColor: Color,
    portColor: Color,
    shadowColor: Color,
    ledColor: Color,
    pulse: Float,
    yaw: Float,
    bob: Float,
    popScale: Float,
) {
    // Body geometry mirrors the comp's extruded shape: 1.62 x 2.24 units with
    // a 0.44 corner radius, scaled to the available height.
    val bodyH = size.height * 0.62f * popScale
    val bodyW = bodyH * (1.62f / 2.24f)
    val depth = bodyW * DEPTH_RATIO
    val cx = size.width / 2f
    val cy = size.height * 0.46f + bob * bodyH * 0.02f

    // Yaw projection: the front face is scaled by cos(yaw) and pushed sideways by
    // half the depth, leaving the extruded edge visible on the opposite side.
    val c = cos(yaw)
    val s = sin(yaw)
    val frontW = bodyW * abs(c)
    val silhouetteW = frontW + depth * abs(s)
    val frontCx = cx + (depth / 2f) * s
    val facing = c > 0f
    // Face detail fades out over the last stretch before edge-on, where a flat
    // drawing of it would otherwise smear.
    val detail = (abs(c) / 0.35f).coerceIn(0f, 1f)

    val top = cy - bodyH / 2f
    // The corner radius is a property of the shell, so only its horizontal half
    // narrows with the projection.
    val silhouetteCorner = CornerRadius(silhouetteW * 0.27f, bodyW * 0.27f)
    val frontCorner = CornerRadius(frontW * 0.27f, bodyW * 0.27f)

    // Contact shadow. It tightens and darkens as the device dips, and narrows with
    // the silhouette as the device turns.
    drawOval(
        color = shadowColor.copy(alpha = (shadowColor.alpha * (1f - bob * 0.22f)).coerceIn(0f, 1f)),
        topLeft = Offset(cx - silhouetteW * 0.52f, cy + bodyH * 0.54f),
        size = Size(silhouetteW * 1.04f, bodyH * 0.07f * (1f - bob * 0.035f)),
    )

    // Extruded side edge — the full silhouette, drawn a shade darker so the front
    // face reads as sitting in front of it.
    drawRoundRect(
        color = lerp(bodyColor, Color.Black, 0.18f),
        topLeft = Offset(cx - silhouetteW / 2f, top),
        size = Size(silhouetteW, bodyH),
        cornerRadius = silhouetteCorner,
    )

    // Front (or, past edge-on, rear) face with a soft top-lit gradient.
    val faceColor = if (facing) bodyColor else lerp(bodyColor, Color.Black, 0.08f)
    drawRoundRect(
        brush = Brush.verticalGradient(
            colors = listOf(faceColor, faceColor.copy(alpha = 0.86f)),
            startY = top,
            endY = top + bodyH,
        ),
        topLeft = Offset(frontCx - frontW / 2f, top),
        size = Size(frontW, bodyH),
        cornerRadius = frontCorner,
    )
    drawRoundRect(
        color = etchColor.copy(alpha = 0.35f),
        topLeft = Offset(frontCx - frontW / 2f, top),
        size = Size(frontW, bodyH),
        cornerRadius = frontCorner,
        style = Stroke(width = 1.dp.toPx()),
    )

    // Side button, on the body's right edge. It sits at mid-depth, so it tracks
    // cos(yaw) alone, and it is hidden once that edge has swung behind the body.
    val buttonAlpha = (0.6f - s * 3f).coerceIn(0f, 1f)
    if (buttonAlpha > 0f) {
        val buttonX = cx + bodyW * 0.5f * c
        drawRoundRect(
            color = lerp(bodyColor, Color.White, 0.1f).copy(alpha = buttonAlpha),
            topLeft = Offset(buttonX - bodyW * 0.022f, cy - bodyH * 0.24f),
            size = Size(bodyW * 0.045f, bodyH * 0.08f),
            cornerRadius = CornerRadius(bodyW * 0.02f, bodyW * 0.02f),
        )
    }

    val ledY = top + bodyH * 0.855f
    val ledR = bodyW * 0.038f

    if (facing && detail > 0f) {
        // Two mic ports near the top, each in an etched ring.
        val micR = bodyW * 0.026f
        val micY = top + bodyH * 0.16f
        listOf(-0.123f, 0.123f).forEach { dx ->
            val mx = frontCx + bodyW * dx * c
            drawEllipse(
                color = etchColor.copy(alpha = detail),
                center = Offset(mx, micY),
                radiusX = micR * 1.5f * abs(c),
                radiusY = micR * 1.5f,
                style = Stroke(micR * 0.4f),
            )
            drawEllipse(
                color = portColor.copy(alpha = detail),
                center = Offset(mx, micY),
                radiusX = micR * abs(c),
                radiusY = micR,
            )
        }

        // Engraved emblem: two concentric rings and a filled dot.
        val emblem = Offset(frontCx, cy)
        drawEllipse(etchColor.copy(alpha = detail), emblem, bodyW * 0.123f * c, bodyW * 0.123f, Stroke(bodyW * 0.021f))
        drawEllipse(etchColor.copy(alpha = detail), emblem, bodyW * 0.065f * c, bodyW * 0.065f, Stroke(bodyW * 0.018f))
        drawEllipse(etchColor.copy(alpha = detail), emblem, bodyW * 0.021f * c, bodyW * 0.021f)
    }

    // The LED halo — drawn even when the device has turned away, at a fraction of
    // its brightness, so the status cue survives a full revolution.
    val haloAlpha = (0.05f + pulse * 0.32f) * if (facing) 1f else 0.45f
    val haloR = ledR * (5f + pulse * 3f)
    drawCircle(
        brush = Brush.radialGradient(
            colors = listOf(ledColor.copy(alpha = haloAlpha), ledColor.copy(alpha = 0f)),
            center = Offset(frontCx, ledY),
            radius = haloR,
        ),
        radius = haloR,
        center = Offset(frontCx, ledY),
    )

    if (facing && detail > 0f) {
        drawEllipse(
            color = etchColor.copy(alpha = detail),
            center = Offset(frontCx, ledY),
            radiusX = ledR * 1.45f * c,
            radiusY = ledR * 1.45f,
            style = Stroke(ledR * 0.32f),
        )
        drawEllipse(
            color = ledColor.copy(alpha = (0.5f + pulse * 0.5f) * detail),
            center = Offset(frontCx, ledY),
            radiusX = ledR * c,
            radiusY = ledR,
        )
    }

    // USB-C port on the bottom edge — part of the shell, so it tracks the
    // silhouette rather than the face.
    drawRoundRect(
        color = portColor,
        topLeft = Offset(cx - silhouetteW * 0.105f, top + bodyH - 1f),
        size = Size(silhouetteW * 0.21f, bodyH * 0.017f),
        cornerRadius = CornerRadius(bodyH * 0.01f, bodyH * 0.01f),
    )
}
