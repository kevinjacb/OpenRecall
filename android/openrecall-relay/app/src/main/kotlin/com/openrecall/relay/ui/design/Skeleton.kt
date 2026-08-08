package com.openrecall.relay.ui.design

import androidx.compose.foundation.background
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier

/**
 * Modifier extension that paints a shimmer-style placeholder surface.
 *
 * **Phase 1 contract:** this is a static placeholder. The actual
 * animation (infinite-transition gradient sweep) is wired in a later
 * phase when the live state lands. Today it just paints the surface
 * variant color so the loading card looks correct in a static
 * screenshot. Callers that want a "skeleton" should still wrap their
 * placeholder in [LoadingCard] (or the same `Modifier.shimmer()`) so
 * the visual contract is preserved.
 *
 * The static color is `surfaceVariant` — a step lighter than the
 * surrounding card so the placeholder reads as "to be filled", not
 * "unrendered".
 *
 * The function is `@Composable` because [MaterialTheme.colorScheme]
 * reads from the active CompositionLocal; calling it from a non-
 * composable Modifier extension would crash at runtime.
 */
@Composable
fun Modifier.shimmer(): Modifier =
    this.background(MaterialTheme.colorScheme.surfaceVariant)
