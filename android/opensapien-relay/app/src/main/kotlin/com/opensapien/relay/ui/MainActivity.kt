package com.opensapien.relay.ui

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import com.opensapien.relay.core.ui.OpenSapienTheme
import com.opensapien.relay.relay.RelayController
import com.opensapien.relay.ui.nav.AppNavigation

/**
 * Single-Activity host for the post-setup UI. The wizard lives in
 * [SetupActivity] (the launcher); once it's done, it launches this Activity
 * (first launch) or returns `RESULT_OK` to it (re-provision from Settings).
 *
 * Renders the monochrome [OpenSapienTheme] and the [AppNavigation] host (NavHost +
 * BottomBar). The Settings tab's "Reconfigure device" launches SetupActivity
 * through [reconfigureLauncher]; on `RESULT_OK` the relay state is refreshed
 * (the server poller picks up the new config on the next poll — the
 * `clientProvider` reads `Config` per call — so no explicit re-init is needed).
 */
class MainActivity : ComponentActivity() {

    private val reconfigureLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK) {
            // Re-emit the current relay state so collectors (Home, Device)
            // re-render with fresh config. The poller's next 2s tick fetches
            // the new server; the session repo's next request uses the new
            // client. Nothing to re-construct.
            RelayController.requestRefresh()
        }
    }

    private fun launchReconfigure() {
        reconfigureLauncher.launch(
            Intent(this, SetupActivity::class.java).putExtra(SetupActivity.EXTRA_RECONFIGURE, true),
        )
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            OpenSapienTheme {
                AppNavigation(onReconfigure = ::launchReconfigure)
            }
        }
    }
}