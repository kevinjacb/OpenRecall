package com.openrecall.relay.ui
import com.openrecall.relay.core.ui.OpenRecallTheme as CoreOpenRecallTheme
import androidx.compose.runtime.Composable

/**
 * Re-export of the core [CoreOpenRecallTheme] at the `ui` package boundary. The
 * setup wizard ([SetupActivity]) imports `com.openrecall.relay.ui.OpenRecallTheme`;
 * deleting this file would break the wizard. The new theme lives at
 * `core.ui.OpenRecallTheme` and is the single source of truth — this is just
 * a thin shim to keep the import path stable.
 *
 * The typealias + function pattern from the brief doesn't compile (Kotlin
 * doesn't allow a typealias and a function with the same name in the same
 * file), so we expose only the function. Type-position callers must use
 * the core type directly.
 */
@Composable
fun OpenRecallTheme(content: @Composable () -> Unit) = CoreOpenRecallTheme(content = content)
