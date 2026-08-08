package com.opensapien.relay.core.ui

import androidx.compose.material3.Typography
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.em
import androidx.compose.ui.unit.sp

/**
 * The type scale from the `design/` comp. The comp is set in Instrument
 * Sans; we don't ship a font binary, so we use the platform sans and carry
 * over what actually defines the look: the *weights* (600 for anything
 * structural), the *sizes*, and the negative tracking on large headings.
 *
 * Slot mapping (M3 name → where the comp uses it):
 *  - displaySmall   → screen hero ("Everything it heard")
 *  - headlineMedium → device name on Home ("Sense")
 *  - headlineSmall  → metric value ("82%")
 *  - titleLarge     → sheet / detail titles
 *  - titleMedium    → app header brand, section headings
 *  - titleSmall     → card titles (session rows)
 *  - bodyLarge      → memory text
 *  - bodyMedium     → body copy, transcript lines
 *  - bodySmall      → supporting copy
 *  - labelLarge     → buttons, settings rows
 *  - labelMedium    → metadata, timestamps
 *  - labelSmall     → uppercase eyebrows, tab labels
 *
 * Components must read `MaterialTheme.typography`, never this value
 * directly, so a per-screen override still works.
 */
private val Sans = FontFamily.SansSerif

private fun style(
    size: Int,
    weight: FontWeight,
    lineHeight: Int,
    tracking: Float = 0f,
) = TextStyle(
    fontFamily = Sans,
    fontSize = size.sp,
    fontWeight = weight,
    lineHeight = lineHeight.sp,
    letterSpacing = tracking.em,
)

private val SemiBold = FontWeight.SemiBold
private val Medium = FontWeight.Medium
private val Normal = FontWeight.Normal

val SenseTypography: Typography = Typography(
    displayLarge = style(34, SemiBold, 40, -0.03f),
    displayMedium = style(30, SemiBold, 36, -0.03f),
    displaySmall = style(28, SemiBold, 34, -0.03f),

    headlineLarge = style(28, SemiBold, 34, -0.025f),
    headlineMedium = style(27, SemiBold, 33, -0.025f),
    headlineSmall = style(23, SemiBold, 28, -0.02f),

    titleLarge = style(17, SemiBold, 23, -0.01f),
    titleMedium = style(15, SemiBold, 21, -0.01f),
    titleSmall = style(15, SemiBold, 20, -0.01f),

    bodyLarge = style(16, Normal, 24),
    bodyMedium = style(15, Normal, 23),
    bodySmall = style(13, Normal, 19),

    labelLarge = style(15, SemiBold, 20),
    labelMedium = style(13, Medium, 17),
    labelSmall = style(12, SemiBold, 16, 0.05f),
)
