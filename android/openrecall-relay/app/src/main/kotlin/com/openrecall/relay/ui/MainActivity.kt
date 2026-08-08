package com.openrecall.relay.ui

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.lifecycle.lifecycleScope
import com.openrecall.relay.core.RecallLog
import com.openrecall.relay.core.ui.OpenRecallTheme
import com.openrecall.relay.data.RepositoryModule
import com.openrecall.relay.relay.RelayController
import com.openrecall.relay.ui.nav.AppNavigation
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch

/**
 * The launcher Activity and single-Activity host for the whole app.
 *
 * Home is the start destination, provisioned or not — a first-run user sees
 * a "Not connected" device with a set-up call to action rather than a bare
 * connect form. The wizard ([SetupActivity]) is now a push from here, via
 * [reconfigureLauncher], reached from Home's CTA and Settings → Reconfigure.
 *
 * Because the wizard is no longer the launcher, it is also no longer the only
 * thing that starts the relay: [startRelayIfProvisioned] does that on every
 * cold start when a device is already paired.
 */
class MainActivity : ComponentActivity() {

    private val reconfigureLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK) {
            // Re-emit the current relay state so collectors (Home, Device)
            // re-render against the fresh config. The status poller picks the
            // new server up on its next tick and the client provider reads
            // Config per call, so there is nothing to re-construct.
            RelayController.requestRefresh()
        }
    }

    private fun launchSetup() {
        reconfigureLauncher.launch(Intent(this, SetupActivity::class.java))
    }

    /**
     * Start the foreground relay service if a device has been provisioned.
     *
     * No intent extras: [com.openrecall.relay.RelayService]'s `onStartCommand`
     * restores the URL, token and gateway port from the persisted config when
     * they are absent, and a start on an already-running service is a
     * no-op re-assertion of its state — so this is safe on every launch.
     */
    private fun startRelayIfProvisioned() {
        lifecycleScope.launch {
            val config = runCatching {
                RepositoryModule.repos.configuration.observe().first()
            }.getOrNull() ?: return@launch
            if (!config.provisioned) return@launch
            runCatching { RepositoryModule.repos.relayStarter.start() }
                .onFailure {
                    RecallLog.w(
                        tag = "MainActivity",
                        msg = "relay auto-start failed: ${it.javaClass.simpleName}",
                    )
                }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdgeLight()
        startRelayIfProvisioned()
        setContent {
            OpenRecallTheme {
                AppNavigation(onReconfigure = ::launchSetup)
            }
        }
    }
}
