package com.opensapien.relay.ui
import com.opensapien.relay.core.ui.OpenSapienTheme as CoreOpenSapienTheme
import androidx.compose.runtime.Composable

/**
 * Re-export of the core [CoreOpenSapienTheme] at the `ui` package boundary. The
 * setup wizard ([SetupActivity]) imports `com.opensapien.relay.ui.OpenSapienTheme`;
 * deleting this file would break the wizard. The new theme lives at
 * `core.ui.OpenSapienTheme` and is the single source of truth — this is just
 * a thin shim to keep the import path stable.
 *
 * The typealias + function pattern from the brief doesn't compile (Kotlin
 * doesn't allow a typealias and a function with the same name in the same
 * file), so we expose only the function. Type-position callers must use
 * the core type directly.
 */
@Composable
fun OpenSapienTheme(content: @Composable () -> Unit) = CoreOpenSapienTheme(content = content)
