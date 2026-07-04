package com.sense.relay.core.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable

/**
 * The Sense Material 3 theme. Monochrome by design — dynamic color is off
 * so the brand doesn't drift based on the user's wallpaper. Single accent
 * (warm graphite-on-ink) is reserved for the live/recording state.
 *
 * Both light and dark variants share the same neutral hues, just inverted
 * luminance, so a screenshot in either mode looks like the same product.
 *
 * `darkTheme` is exposed for tests + explicit overrides; production
 * callers should omit it and let the system flag decide.
 */
@Composable
fun SenseTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    val colors = if (darkTheme) {
        darkColorScheme(
            background = SensePalette.BackgroundDark,
            surface = SensePalette.SurfaceDark,
            onBackground = SensePalette.OnSurfaceDark,
            onSurface = SensePalette.OnSurfaceDark,
            onSurfaceVariant = SensePalette.OnSurfaceMutedDark,
            outline = SensePalette.OutlineDark,
            primary = SensePalette.AccentDark,
            onPrimary = SensePalette.BackgroundDark,
        )
    } else {
        lightColorScheme(
            background = SensePalette.BackgroundLight,
            surface = SensePalette.SurfaceLight,
            onBackground = SensePalette.OnSurfaceLight,
            onSurface = SensePalette.OnSurfaceLight,
            onSurfaceVariant = SensePalette.OnSurfaceMutedLight,
            outline = SensePalette.OutlineLight,
            primary = SensePalette.AccentLight,
            onPrimary = SensePalette.OnSurfaceLight,
        )
    }
    MaterialTheme(
        colorScheme = colors,
        typography = SenseTypography,
        shapes = SenseShapes,
        content = content,
    )
}
