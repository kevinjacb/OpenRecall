package com.openrecall.relay.ui.design

import com.openrecall.relay.core.ui.OpenRecallTheme
import kotlin.test.Test
import kotlin.test.assertNotNull

/**
 * The theme test is deliberately light-touch: we only assert that the
 * composable function reference exists and is callable, since Compose UI
 * composables don't run in plain JVM unit tests. Snapshot tests belong to a
 * later visual-regression phase; today the value is "the code path compiles
 * and is reachable from MainActivity".
 */
class OpenRecallThemeTest {

    @Test fun themeComposableReferenceExists() {
        // Touch the symbol so the import is meaningful and a missing function
        // surfaces as a compile error.
        val ref: @androidx.compose.runtime.Composable () -> Unit = { OpenRecallTheme {} }
        assertNotNull(ref)
    }
}
