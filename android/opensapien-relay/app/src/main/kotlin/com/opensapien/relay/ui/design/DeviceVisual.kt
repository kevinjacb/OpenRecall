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
import androidx.compose.ui.graphics.drawscope.scale
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

// ── Palette ────────────────────────────────────────────────────────────────
// Every colour below is a material from the comp's three.js scene, taken at its
// literal value. The locket is one physical object, so it keeps these colours in
// both themes — only the wash behind it follows the theme.

/** `MeshPhysicalMaterial` on the extruded body. */
private val BodyShell = Color(0xFFF7F5F1)

/** The `dark` material — mic ports and the USB-C cutout. */
private val PortDark = Color(0xFF232326)

/** The `etch` material — engraved rings and the emblem. */
private val EtchGrey = Color(0xFFA39D94)

/** The ring around the status LED. */
private val LedRingGrey = Color(0xFF9C9891)

/** The side button's own, slightly warmer shell material. */
private val ButtonShell = Color(0xFFECEAE5)

/** The contact-shadow texture's ink. */
private val ShadowInk = Color(0xFF1C1A18)

private val LedGreen = Color(0xFF3FBF7F)
private val LedAmber = Color(0xFFE8813F)
private val LedGrey = Color(0xFF8D8B88)

// The halo sprite is tinted a shade lighter than the LED itself.
private val GlowGreen = Color(0xFF54D896)
private val GlowAmber = Color(0xFFF59B5C)
private val GlowGrey = Color(0xFF8D8B88)

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

// ── Geometry ───────────────────────────────────────────────────────────────
// Scene units from the comp, verbatim. Everything is drawn as `unit * u`, where
// `u` is the pixel size of one scene unit, so the proportions cannot drift.

private const val BODY_W = 1.62f
private const val BODY_H = 2.24f
private const val BODY_D = 0.30f
private const val BODY_CORNER = 0.44f

private const val MIC_X = 0.20f
private const val MIC_Y = 0.80f
private const val MIC_R = 0.042f
private const val MIC_RING_R = 0.058f
private const val MIC_RING_TUBE = 0.011f

private const val EMBLEM_Y = 0.02f
private const val EMBLEM_R = 0.20f
private const val EMBLEM_TUBE = 0.017f
private const val EMBLEM_INNER_R = 0.105f
private const val EMBLEM_INNER_TUBE = 0.015f
private const val EMBLEM_DOT_R = 0.034f

private const val LED_Y = -0.80f
private const val LED_R = 0.062f
private const val LED_RING_R = 0.072f
private const val LED_RING_TUBE = 0.010f

private const val BUTTON_X = BODY_W / 2f + 0.045f
private const val BUTTON_Y = 0.42f
private const val BUTTON_R = 0.052f
private const val BUTTON_LEN = 0.055f

private const val PORT_W = 0.34f
private const val PORT_H = 0.075f
private const val PORT_Y = -(BODY_H / 2f) - 0.012f

private const val SHADOW_Y = -1.48f

/**
 * The hero device illustration.
 *
 * The comp renders the locket with a live three.js scene (`design/sense-device.js`):
 * it turns on its own, can be grabbed and spun with inertia, bobs as if suspended,
 * and pops when the connection state changes. Shipping that scene as-is means a 3D
 * pipeline (Filament or SceneView) plus a `.glb` asset, neither of which exists in
 * this repo yet.
 *
 * This is a hand-drawn 2.5D stand-in that reproduces the comp's shape, colour and
 * motion on a Compose canvas. Every dimension is one of the scene's own constants
 * (see the geometry block above) times the pixel size of one scene unit, and every
 * colour is one of its materials at its literal value — so the locket looks the same
 * in both themes, because it is one physical object; only the wash behind it follows
 * the theme. Motion matches too: the same per-state yaw speeds, drag-with-inertia
 * feel, 1.05rad/s float, breathing contact shadow and scale pop on a state change.
 *
 * Rotation is faked by projecting the body — the front face narrows with `cos(yaw)`
 * while the extruded side edge widens with `sin(yaw)` — so the silhouette turns
 * without a real mesh. Face detail (mics, emblem, LED) fades out as the device turns
 * edge-on; the LED halo stays faintly visible from behind so the status cue never
 * disappears entirely. Swapping in the real model later is a change to this one
 * composable.
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
                ledColor = state.ledColor(),
                glowColor = state.glowColor(),
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

private fun DeviceVisualState.glowColor(): Color = when (this) {
    DeviceVisualState.Connected -> GlowGreen
    DeviceVisualState.Searching -> GlowAmber
    DeviceVisualState.Off -> GlowGrey
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

/** The comp scales a material's colour rather than fading it; so does this. */
private fun Color.dim(factor: Float) = copy(red = red * factor, green = green * factor, blue = blue * factor)

private fun DrawScope.drawLocket(
    ledColor: Color,
    glowColor: Color,
    pulse: Float,
    yaw: Float,
    bob: Float,
    popScale: Float,
) {
    // One scene unit, in pixels. Every measurement below is one of the comp's own
    // scene constants times `u`, so the silhouette and the placement of each
    // feature are the comp's, at whatever size the layout gives us.
    val u = (size.height * 0.62f / BODY_H) * popScale
    val cx = size.width / 2f
    val groundY = size.height * 0.46f
    // The body floats by the comp's 0.045 units; the ground under it does not.
    val cy = groundY - bob * 0.045f * u

    // Yaw projection. The front face narrows with cos(yaw) and slides half a body
    // depth sideways, which leaves the extruded side edge showing on the far side.
    val c = cos(yaw)
    val s = sin(yaw)
    val facing = c > 0f
    val bodyH = BODY_H * u
    val faceW = BODY_W * abs(c) * u
    val shellW = faceW + BODY_D * abs(s) * u
    val faceCx = cx + (BODY_D / 2f) * s * u
    val top = cy - bodyH / 2f
    // Face detail fades over the last stretch before edge-on, where drawing it
    // flat would only smear it.
    val detail = (abs(c) / 0.35f).coerceIn(0f, 1f)

    // Scene coordinates → screen. Face features sit on the front plane, so they
    // ride `faceCx`; y is scene-up, screen-down.
    fun faceX(x: Float) = faceCx + x * c * u
    fun sceneY(y: Float) = cy - y * u

    // Contact shadow — the comp's radial texture, flattened onto the ground plane.
    // Its opacity and size breathe on the same phase as the float.
    val shadowFade = 0.85f - bob * 0.09f
    val shadowR = 1.5f * u * (1f - bob * 0.035f)
    val shadowCenter = Offset(cx, groundY - SHADOW_Y * u)
    scale(scaleX = 1f, scaleY = 0.14f, pivot = shadowCenter) {
        drawCircle(
            brush = Brush.radialGradient(
                colorStops = arrayOf(
                    0f to ShadowInk.copy(alpha = 0.42f * shadowFade),
                    0.45f to ShadowInk.copy(alpha = 0.16f * shadowFade),
                    1f to ShadowInk.copy(alpha = 0f),
                ),
                center = shadowCenter,
                radius = shadowR,
            ),
            radius = shadowR,
            center = shadowCenter,
        )
    }

    // The shell: the whole silhouette, front face plus the exposed extruded edge.
    // It is a shade darker so the face reads as standing in front of it.
    drawRoundRect(
        color = lerp(BodyShell, ShadowInk, 0.18f),
        topLeft = Offset(cx - shellW / 2f, top),
        size = Size(shellW, bodyH),
        cornerRadius = CornerRadius(BODY_CORNER * shellW / BODY_W, BODY_CORNER * u),
    )

    // The face itself — front when it is turned towards us, back when it is not.
    // The gradient is the scene's key light; the base colour is the comp's shell
    // material untouched at the middle of the body.
    val faceCorner = CornerRadius(BODY_CORNER * abs(c) * u, BODY_CORNER * u)
    val faceTop = if (facing) lerp(BodyShell, Color.White, 0.06f) else lerp(BodyShell, ShadowInk, 0.04f)
    val faceBottom = lerp(BodyShell, ShadowInk, if (facing) 0.10f else 0.16f)
    drawRoundRect(
        brush = Brush.verticalGradient(listOf(faceTop, faceBottom), startY = top, endY = top + bodyH),
        topLeft = Offset(faceCx - faceW / 2f, top),
        size = Size(faceW, bodyH),
        cornerRadius = faceCorner,
    )
    drawRoundRect(
        color = EtchGrey.copy(alpha = 0.35f),
        topLeft = Offset(faceCx - faceW / 2f, top),
        size = Size(faceW, bodyH),
        cornerRadius = faceCorner,
        style = Stroke(width = 1.dp.toPx()),
    )

    // Side button: a stub on the right edge at mid-depth, so it tracks cos(yaw)
    // alone and disappears once that edge has swung behind the body.
    val buttonAlpha = (0.6f - s * 3f).coerceIn(0f, 1f)
    if (buttonAlpha > 0f) {
        val buttonW = BUTTON_LEN * abs(c) * u
        drawRoundRect(
            color = ButtonShell.copy(alpha = buttonAlpha),
            topLeft = Offset(cx + BUTTON_X * c * u - buttonW / 2f, sceneY(BUTTON_Y) - BUTTON_R * u),
            size = Size(buttonW, BUTTON_R * 2f * u),
            cornerRadius = CornerRadius(BUTTON_R * u * 0.5f, BUTTON_R * u * 0.5f),
        )
    }

    if (facing && detail > 0f) {
        // Two mic ports, each in an etched ring.
        listOf(-MIC_X, MIC_X).forEach { x ->
            val centre = Offset(faceX(x), sceneY(MIC_Y))
            drawEllipse(
                color = EtchGrey.copy(alpha = detail),
                center = centre,
                radiusX = MIC_RING_R * c * u,
                radiusY = MIC_RING_R * u,
                style = Stroke(MIC_RING_TUBE * 2f * u),
            )
            drawEllipse(
                color = PortDark.copy(alpha = detail),
                center = centre,
                radiusX = MIC_R * c * u,
                radiusY = MIC_R * u,
            )
        }

        // Engraved emblem: two concentric rings and a filled dot.
        val emblem = Offset(faceX(0f), sceneY(EMBLEM_Y))
        val etch = EtchGrey.copy(alpha = detail)
        drawEllipse(etch, emblem, EMBLEM_R * c * u, EMBLEM_R * u, Stroke(EMBLEM_TUBE * 2f * u))
        drawEllipse(etch, emblem, EMBLEM_INNER_R * c * u, EMBLEM_INNER_R * u, Stroke(EMBLEM_INNER_TUBE * 2f * u))
        drawEllipse(etch, emblem, EMBLEM_DOT_R * c * u, EMBLEM_DOT_R * u)
    }

    // The halo sprite. It stays lit at a fraction of its brightness once the body
    // has turned away, so the status cue survives a full revolution.
    val ledCentre = Offset(faceX(0f), sceneY(LED_Y))
    val haloOpacity = (0.05f + pulse * 0.32f) * if (facing) 1f else 0.45f
    val haloR = (0.22f + pulse * 0.08f) * u
    drawCircle(
        brush = Brush.radialGradient(
            colorStops = arrayOf(
                0f to glowColor.copy(alpha = 0.42f * haloOpacity),
                0.25f to glowColor.copy(alpha = 0.20f * haloOpacity),
                1f to glowColor.copy(alpha = 0f),
            ),
            center = ledCentre,
            radius = haloR,
        ),
        radius = haloR,
        center = ledCentre,
    )

    if (facing && detail > 0f) {
        drawEllipse(
            color = LedRingGrey.copy(alpha = detail),
            center = ledCentre,
            radiusX = LED_RING_R * c * u,
            radiusY = LED_RING_R * u,
            style = Stroke(LED_RING_TUBE * 2f * u),
        )
        drawEllipse(
            color = ledColor.dim(0.5f + pulse * 0.5f).copy(alpha = detail),
            center = ledCentre,
            radiusX = LED_R * c * u,
            radiusY = LED_R * u,
        )
    }

    // USB-C cutout, straddling the bottom edge. It belongs to the shell, so it
    // narrows with the silhouette rather than with the face.
    val portW = PORT_W * shellW / BODY_W
    drawRoundRect(
        color = PortDark,
        topLeft = Offset(cx - portW / 2f, sceneY(PORT_Y) - PORT_H * u / 2f),
        size = Size(portW, PORT_H * u),
        cornerRadius = CornerRadius(PORT_H * u * 0.4f, PORT_H * u * 0.4f),
    )
}
