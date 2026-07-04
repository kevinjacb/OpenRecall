package com.sense.relay.ui

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import com.sense.relay.core.ui.SenseTheme
import com.sense.relay.ui.nav.AppNavigation

/**
 * Single-Activity host for the post-setup UI. The wizard lives in
 * [SetupActivity] (the launcher); once it's done, it launches this
 * Activity. Phase 1 doesn't wire that launch — it's a Phase 7 task —
 * so this Activity is reachable only via an explicit Intent in this
 * slice.
 *
 * Renders the monochrome [SenseTheme] and the [AppNavigation] host
 * (NavHost + BottomBar). The 4 destinations are placeholders today;
 * real screens land in Phase 4+.
 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            SenseTheme {
                AppNavigation()
            }
        }
    }
}
