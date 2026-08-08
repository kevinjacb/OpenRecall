package com.opensapien.relay.core.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.ReadOnlyComposable

/**
 * The OpenSapien theme. Two colour systems live side by side, deliberately:
 *
 *  - `MaterialTheme.colorScheme` — so stock M3 components (TextField,
 *    Snackbar, Dialog) pick up the right colours without per-call-site
 *    overrides.
 *  - [SenseTheme.colors] — the richer, design-comp-accurate token set
 *    ([SenseColors]) that our own components read. M3's scheme has no slot
 *    for "accent chip fill" or "healthy-status pill", so those live here.
 *
 * Dynamic colour is off: the brand must not drift with the user's wallpaper.
 *
 * `darkTheme` is exposed for tests and previews; production callers omit it
 * and let the system flag decide.
 */
@Composable
fun OpenSapienTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    val sense = if (darkTheme) SenseColors.dark() else SenseColors.light()
    val scheme = if (darkTheme) {
        darkColorScheme(
            primary = sense.accent,
            onPrimary = sense.canvas,
            primaryContainer = sense.accentSoft,
            onPrimaryContainer = sense.accentInk,
            secondary = sense.ok,
            onSecondary = sense.canvas,
            background = sense.canvas,
            onBackground = sense.ink,
            surface = sense.card,
            onSurface = sense.ink,
            surfaceVariant = sense.canvasSunken,
            onSurfaceVariant = sense.grey,
            outline = sense.border,
            outlineVariant = sense.divider,
            error = sense.danger,
        )
    } else {
        lightColorScheme(
            primary = sense.accent,
            onPrimary = sense.card,
            primaryContainer = sense.accentSoft,
            onPrimaryContainer = sense.accentInk,
            secondary = sense.ok,
            onSecondary = sense.card,
            background = sense.canvas,
            onBackground = sense.ink,
            surface = sense.card,
            onSurface = sense.ink,
            surfaceVariant = sense.canvasSunken,
            onSurfaceVariant = sense.grey,
            outline = sense.border,
            outlineVariant = sense.divider,
            error = sense.danger,
        )
    }
    CompositionLocalProvider(LocalSenseColors provides sense) {
        MaterialTheme(
            colorScheme = scheme,
            typography = SenseTypography,
            shapes = SenseShapes,
            content = content,
        )
    }
}

/**
 * Accessor for the design-system tokens that M3's `colorScheme` can't
 * express. Mirrors the `MaterialTheme` object convention so call sites read
 * `SenseTheme.colors.accentSoft` next to `MaterialTheme.typography.titleSmall`.
 */
object SenseTheme {
    val colors: SenseColors
        @Composable
        @ReadOnlyComposable
        get() = LocalSenseColors.current
}
