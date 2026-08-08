package com.opensapien.relay.core.ui

import androidx.compose.material3.Typography

/**
 * The Material 3 type scale, pinned to a small set of weights/sizes. We
 * don't ship custom fonts in this slice — the brief says "Material 3
 * defaults, constrained". A future pass may replace this with a
 * hand-tuned scale once design lands.
 *
 * Components should always read from `MaterialTheme.typography` rather
 * than this constant directly, so a per-screen override (large-display
 * mode, accessibility scale) works without changes downstream.
 */
val SenseTypography: Typography = Typography()
