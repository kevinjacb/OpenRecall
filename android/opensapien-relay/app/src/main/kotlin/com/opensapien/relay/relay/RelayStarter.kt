package com.opensapien.relay.relay

import android.content.Context
import android.content.Intent
import androidx.core.content.ContextCompat
import com.opensapien.relay.RelayService

/**
 * Restarts the foreground [RelayService] — the "Retry connection" escape hatch.
 *
 * Today the only place the service is started is the setup wizard
 * ([com.opensapien.relay.ui.SetupActivity]). When the BLE/WS link drops, the
 * service `stopSelf()`s, leaving the user unable to reconnect short of
 * re-running setup. This seam lets a ViewModel re-launch the service without
 * holding an Android [Context] (ViewModels must not retain contexts).
 *
 * Re-launching with **no intent extras** is deliberate: [RelayService]'s
 * `onStartCommand` restores `server_url` / `token` from [com.opensapien.relay.store.ServerConfig]
 * when the intent carries no extras — the existing config-redelivery path —
 * so a reconnect reuses the last-good provisioning instead of forcing a
 * reconfigure. A fresh generation guard in the service invalidates any
 * stale callbacks from the previous run.
 *
 * The interface exists so ViewModels (and their host tests) can depend on an
 * injectable, fakeable seam instead of a concrete [Context].
 */
interface RelayStarter {
    /** Re-launch [RelayService] from its current config. Idempotent: a start
     *  while the service is already running is a no-op delivery (the service
     *  handles a redundant `onStartCommand` by re-asserting its state). */
    fun start()
}

/**
 * Production [RelayStarter]: starts [RelayService] as a foreground service via
 * [ContextCompat.startForegroundService] (the Android 8+ API for starting a
 * foreground service from a backgrounded context — e.g. a "Retry connection"
 * tap while the app is foregrounded but the service has stopped itself).
 */
class IntentRelayStarter(private val context: Context) : RelayStarter {
    override fun start() {
        ContextCompat.startForegroundService(context, Intent(context, RelayService::class.java))
    }
}