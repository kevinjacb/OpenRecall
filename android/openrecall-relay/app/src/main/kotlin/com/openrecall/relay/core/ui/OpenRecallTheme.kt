package com.openrecall.relay.core.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.ReadOnlyComposable

/**
 * The OpenRecall theme. Two colour systems live side by side, deliberately:
 *
 *  - `MaterialTheme.colorScheme` — so stock M3 components (TextField,
 *    Snackbar, Dialog) pick up the right colours without per-call-site
 *    overrides.
 *  - [RecallTheme.colors] — the richer, design-comp-accurate token set
 *    ([RecallColors]) that our own components read. M3's scheme has no slot
 *    for "accent chip fill" or "healthy-status pill", so those live here.
 *
 * Dynamic colour is off: the brand must not drift with the user's wallpaper.
 *
 * **The app is light-only.** `darkTheme` defaults to `false` rather than
 * `isSystemInDarkTheme()`, so a phone in dark mode still gets the light comp.
 * The dark palette is kept and still reachable by passing `darkTheme = true`
 * (tests and previews do), but nothing in production sets it.
 *
 * Forcing light here is only one of three layers — the platform window theme
 * (`res/values/themes.xml`, with no `values-night` counterpart and force-dark
 * disabled) and the system-bar icon style (`enableEdgeToEdge` in the
 * Activities) have to agree, or you get a light app under dark system bars
 * whose icons are invisible.
 */
@Composable
fun OpenRecallTheme(
    darkTheme: Boolean = false,
    content: @Composable () -> Unit,
) {
    val sense = if (darkTheme) RecallColors.dark() else RecallColors.light()
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
            typography = RecallTypography,
            shapes = RecallShapes,
            content = content,
        )
    }
}

/**
 * Accessor for the design-system tokens that M3's `colorScheme` can't
 * express. Mirrors the `MaterialTheme` object convention so call sites read
 * `RecallTheme.colors.accentSoft` next to `MaterialTheme.typography.titleSmall`.
 */
object RecallTheme {
    val colors: RecallColors
        @Composable
        @ReadOnlyComposable
        get() = LocalSenseColors.current
}
