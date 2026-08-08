package com.openrecall.relay.core.ui

import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color

/**
 * Raw palette tokens, taken verbatim from the `design/` comp
 * (`Sense Relay.dc.html`). Warm off-white canvas, pure-white cards on a
 * hairline border, near-black text, and a single burnt-orange accent.
 * A second (green) hue is reserved for "healthy/connected" affordances —
 * it is a *status* colour, never decoration.
 *
 * Nothing outside this file should hard-code a hex value. Components read
 * the semantic [RecallColors] tokens below via [LocalSenseColors].
 */
private object Palette {
    // --- Neutrals (light) ---
    val Canvas = Color(0xFFFBFAF8)          // screen background
    val CanvasSunken = Color(0xFFEFEDE8)    // behind the phone frame / inset wells
    val Card = Color(0xFFFFFFFF)
    val Border = Color(0xFFEDEAE5)
    val Divider = Color(0xFFF4F1EC)
    val Ink = Color(0xFF17181A)             // primary text + "filled" buttons
    val InkSoft = Color(0xFF2B2D31)         // body copy inside cards
    val InkMuted = Color(0xFF4A4E54)        // secondary body
    val Slate = Color(0xFF75797F)           // supporting labels
    val Grey = Color(0xFF8B8F95)            // field labels, metadata
    val GreyLight = Color(0xFFA2A6AC)       // timestamps, hints
    val GreyFaint = Color(0xFFB0B4BA)       // least-important metadata
    val Track = Color(0xFFE7E3DD)           // meter/waveform track
    val TrackOff = Color(0xFFE2DED8)        // switch track, off

    // --- Accent (burnt orange) ---
    val Accent = Color(0xFFD9642F)
    val AccentInk = Color(0xFFA34D1E)       // accent text on a soft accent field
    val AccentSoft = Color(0xFFFBEFE7)      // chip / badge background
    val AccentSofter = Color(0xFFFDF4EE)    // proactive bubble background
    val AccentBorder = Color(0xFFF3E2D5)
    val AccentTab = Color(0xFFF5E7DC)       // selected tab pill

    // --- Status ---
    val Ok = Color(0xFF2F9E68)
    val OkInk = Color(0xFF1F7A50)
    val OkSoft = Color(0xFFEAF4EE)
    val Danger = Color(0xFFC4462F)

    // --- Neutrals (dark) ---
    val CanvasDark = Color(0xFF121212)
    val CanvasSunkenDark = Color(0xFF0B0B0C)
    val CardDark = Color(0xFF1C1C1E)
    val BorderDark = Color(0xFF2A2A2D)
    val DividerDark = Color(0xFF232326)
    val InkDark = Color(0xFFF4F2EF)
    val InkSoftDark = Color(0xFFE3E1DD)
    val InkMutedDark = Color(0xFFBFC2C7)
    val SlateDark = Color(0xFF9DA1A7)
    val GreyDark = Color(0xFF8B8F95)
    val GreyLightDark = Color(0xFF74787E)
    val GreyFaintDark = Color(0xFF5F636A)
    val TrackDark = Color(0xFF323236)

    val AccentDark = Color(0xFFE8823F)
    val AccentInkDark = Color(0xFFF0A878)
    val AccentSoftDark = Color(0xFF2E211A)
    val AccentSofterDark = Color(0xFF261C16)
    val AccentBorderDark = Color(0xFF3D2A1F)

    val OkDark = Color(0xFF44B87E)
    val OkInkDark = Color(0xFF7BD3A6)
    val OkSoftDark = Color(0xFF16261D)
    val DangerDark = Color(0xFFE0705A)
}

/**
 * Semantic colour tokens. Named for *what they mean*, not what they look
 * like, so the dark variant is a straight substitution and a future palette
 * change is one file.
 *
 * Read them from composables via `RecallTheme.colors` — see [LocalSenseColors].
 */
data class RecallColors(
    /** Screen background. */
    val canvas: Color,
    /** Inset wells and the area behind sheets. */
    val canvasSunken: Color,
    /** Card / raised-surface fill. */
    val card: Color,
    /** Hairline card border — the design leans on borders, not shadows. */
    val border: Color,
    /** Divider between rows inside a grouped card. */
    val divider: Color,
    /** Headline text and filled-button backgrounds. */
    val ink: Color,
    /** Body copy. */
    val inkSoft: Color,
    /** Secondary body copy. */
    val inkMuted: Color,
    /** Supporting labels. */
    val slate: Color,
    /** Field labels and metadata. */
    val grey: Color,
    /** Timestamps and hints. */
    val greyLight: Color,
    /** Least-important metadata. */
    val greyFaint: Color,
    /** Meter / waveform track. */
    val track: Color,
    /** Switch track in the off position. */
    val trackOff: Color,
    /** The single brand accent. */
    val accent: Color,
    /** Accent text on [accentSoft]. */
    val accentInk: Color,
    /** Accent chip / badge fill. */
    val accentSoft: Color,
    /** Proactive-message fill (a lighter [accentSoft]). */
    val accentSofter: Color,
    /** Border for accent-tinted surfaces. */
    val accentBorder: Color,
    /** Selected bottom-tab pill. */
    val accentTab: Color,
    /** Healthy / connected. */
    val ok: Color,
    /** Text on [okSoft]. */
    val okInk: Color,
    /** Healthy-status pill fill. */
    val okSoft: Color,
    /** Destructive actions. */
    val danger: Color,
    /** True for the dark variant — for the rare draw call that must branch. */
    val isDark: Boolean,
) {
    companion object {
        fun light() = RecallColors(
            canvas = Palette.Canvas,
            canvasSunken = Palette.CanvasSunken,
            card = Palette.Card,
            border = Palette.Border,
            divider = Palette.Divider,
            ink = Palette.Ink,
            inkSoft = Palette.InkSoft,
            inkMuted = Palette.InkMuted,
            slate = Palette.Slate,
            grey = Palette.Grey,
            greyLight = Palette.GreyLight,
            greyFaint = Palette.GreyFaint,
            track = Palette.Track,
            trackOff = Palette.TrackOff,
            accent = Palette.Accent,
            accentInk = Palette.AccentInk,
            accentSoft = Palette.AccentSoft,
            accentSofter = Palette.AccentSofter,
            accentBorder = Palette.AccentBorder,
            accentTab = Palette.AccentTab,
            ok = Palette.Ok,
            okInk = Palette.OkInk,
            okSoft = Palette.OkSoft,
            danger = Palette.Danger,
            isDark = false,
        )

        fun dark() = RecallColors(
            canvas = Palette.CanvasDark,
            canvasSunken = Palette.CanvasSunkenDark,
            card = Palette.CardDark,
            border = Palette.BorderDark,
            divider = Palette.DividerDark,
            ink = Palette.InkDark,
            inkSoft = Palette.InkSoftDark,
            inkMuted = Palette.InkMutedDark,
            slate = Palette.SlateDark,
            grey = Palette.GreyDark,
            greyLight = Palette.GreyLightDark,
            greyFaint = Palette.GreyFaintDark,
            track = Palette.TrackDark,
            trackOff = Palette.TrackDark,
            accent = Palette.AccentDark,
            accentInk = Palette.AccentInkDark,
            accentSoft = Palette.AccentSoftDark,
            accentSofter = Palette.AccentSofterDark,
            accentBorder = Palette.AccentBorderDark,
            accentTab = Palette.AccentSoftDark,
            ok = Palette.OkDark,
            okInk = Palette.OkInkDark,
            okSoft = Palette.OkSoftDark,
            danger = Palette.DangerDark,
            isDark = true,
        )
    }
}

/**
 * The active [RecallColors]. Provided by
 * [com.openrecall.relay.core.ui.OpenRecallTheme]; read through
 * [RecallTheme.colors] rather than this local directly.
 *
 * `static` because the palette never changes within a composition tree
 * except on a theme switch, which recomposes everything anyway.
 */
val LocalSenseColors = staticCompositionLocalOf { RecallColors.light() }
