package com.opensapien.relay

import android.content.Context
import android.content.Intent
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Test
import org.junit.runner.RunWith

/**
 * On-device smoke test for the relay foreground service — the app's *only* entry point
 * (there is no launcher Activity; the manifest ships just the non-exported [RelayService]).
 *
 * Drives exactly the start path the README documents:
 *   startForegroundService(Intent(ctx, RelayService::class.java).putExtra("server_url", ws))
 *
 * The emulator has no Bluetooth radio, so this cannot reach a real OpenSapien device and the
 * device -> server WebSocket path is not exercised here. It *does* run the previously
 * unverified Service wiring on a real device image: onStartCommand, the foreground
 * notification + channel, and SensorLink construction + start() (the BLE scan). A clean
 * pass means the service starts and survives the no-BLE environment without crashing.
 */
@RunWith(AndroidJUnit4::class)
class RelayServiceStartTest {
    @Test
    fun startsForegroundServiceWithoutCrashing() {
        val ctx: Context = InstrumentationRegistry.getInstrumentation().targetContext
        val intent = Intent(ctx, RelayService::class.java)
            .putExtra("server_url", "ws://10.0.2.2:8765")
        ctx.startForegroundService(intent)
        // Let onStartCommand run: post the foreground notification + kick off SensorLink.
        Thread.sleep(4_000)
    }
}