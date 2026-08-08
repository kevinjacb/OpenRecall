package com.opensapien.relay.core.ui

import androidx.compose.ui.graphics.Color

/**
 * Monochrome palette tokens. Single accent (warm graphite-on-ink) is
 * reserved for the live/recording state per the spec — red would break the
 * monochrome contract. Light and dark variants. Dynamic color is off
 * deliberately (see [OpenSapienTheme]).
 *
 * Hue is intentionally close to neutral. Don't introduce chroma here
 * without a deliberate design review.
 */
object SensePalette {
    // --- Light ---
    val BackgroundLight = Color(0xFFF7F7F7)
    val SurfaceLight = Color(0xFFFFFFFF)
    val OnSurfaceLight = Color(0xFF111111)
    val OnSurfaceMutedLight = Color(0xFF6B6B6B)
    val OutlineLight = Color(0xFFE0E0E0)
    val AccentLight = Color(0xFFB85C2C)   // warm graphite-on-ink, light variant

    // --- Dark ---
    val BackgroundDark = Color(0xFF0E0E0F)
    val SurfaceDark = Color(0xFF18181A)
    val OnSurfaceDark = Color(0xFFF2F2F2)
    val OnSurfaceMutedDark = Color(0xFF9C9C9C)
    val OutlineDark = Color(0xFF2A2A2D)
    val AccentDark = Color(0xFFE89A6A)    // same hue, lifted for dark contrast
}

/**
 * Semantic token names. Components import these (e.g.
 * `SenseColors.OnSurface`) so a future palette swap is a single file
 * change. The static `CompositionLocal` lookup is overkill for a single
 * theme; we expose them as a `data class` and let `OpenSapienTheme` pass the
 * current variant down via parameters.
 */
data class SenseColors(
    val background: Color,
    val surface: Color,
    val onSurface: Color,
    val onSurfaceMuted: Color,
    val outline: Color,
    val accent: Color,
) {
    companion object {
        fun light() = SenseColors(
            background = SensePalette.BackgroundLight,
            surface = SensePalette.SurfaceLight,
            onSurface = SensePalette.OnSurfaceLight,
            onSurfaceMuted = SensePalette.OnSurfaceMutedLight,
            outline = SensePalette.OutlineLight,
            accent = SensePalette.AccentLight,
        )

        fun dark() = SenseColors(
            background = SensePalette.BackgroundDark,
            surface = SensePalette.SurfaceDark,
            onSurface = SensePalette.OnSurfaceDark,
            onSurfaceMuted = SensePalette.OnSurfaceMutedDark,
            outline = SensePalette.OutlineDark,
            accent = SensePalette.AccentDark,
        )
    }
}
