package com.sense.relay

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.IBinder
import android.util.Log
import com.sense.relay.ble.SensorLink
import com.sense.relay.net.ServerSocket
import com.sense.relay.net.wsGatewayUrl
import com.sense.relay.relay.DeviceState
import com.sense.relay.relay.RelayConnectionState
import com.sense.relay.relay.RelayController
import com.sense.relay.relay.ServerState
import com.sense.relay.store.ServerConfig
import kotlinx.coroutines.runBlocking
import java.util.UUID

/**
 * Foreground service that runs the relay: BLE device <-> WebSocket server.
 *
 * Lifecycle: scan/connect device -> open socket -> send §E hello -> bridge. It owns
 * only wiring and I/O; every protocol decision comes from [RelaySession] (the tested
 * brain). Start with an intent extra "server_url", e.g. ws://192.168.1.20:8765.
 *
 * **Re-provision (Phase 7):** `onStartCommand` can be re-delivered on an
 * already-running instance (Settings → Reconfigure re-launches the wizard,
 * which `startForegroundService`s this service with new `server_url`/`token`).
 * A re-delivery tears down the prior session/sensor/socket before
 * re-initializing so the old WebSocket (to the old server) and the old BLE
 * scan don't leak alongside the new ones. The teardown is generation-guarded:
 * each `onStartCommand` increments [generation], and the per-call listeners
 * capture that generation and ignore callbacks from a prior generation —
 * crucially the async `SensorLink.stop()` → `onDisconnected` (fired on the
 * GATT thread after `stop()` returns) of the OLD sensor, which would
 * otherwise call `teardown()` on the NEW session and `stopSelf()` the service.
 */
class RelayService : Service() {

    private companion object {
        const val TAG = "RelayService"
        const val CHANNEL = "sense_relay"
    }

    private lateinit var session: RelaySession
    private lateinit var sensor: SensorLink
    private var socket: ServerSocket? = null
    private var serverUrl: String = "ws://10.0.2.2:8765"  // host loopback from emulator
    private var token: String = ""
    // The WS gateway port the server advertised on /health (separate from the
    // HTTP API port in serverUrl). Null → fall back to the legacy same-port
    // scheme-swap (older server / emulator default). See wsGatewayUrl().
    private var gatewayPort: Int? = null

    /**
     * Bumped on every `onStartCommand`. Per-call listeners capture the value
     * and ignore callbacks whose generation is stale — the guard that makes
     * re-provision teardown safe (see the class KDoc).
     */
    @Volatile
    private var generation = 0

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val urlExtra = intent?.getStringExtra("server_url")
        val tokenExtra = intent?.getStringExtra("token")
        val gatewayPortExtra = intent?.getIntExtra("gateway_port", -1)?.takeIf { it > 0 }
        if (urlExtra != null) serverUrl = urlExtra
        if (tokenExtra != null) token = tokenExtra
        if (gatewayPortExtra != null) gatewayPort = gatewayPortExtra
        // START_STICKY redelivery: intent extras are null — restore from the persisted
        // config (mirroring the token fallback). Only adopt a non-empty persisted value so we
        // never overwrite a valid default with an empty one. gatewayPort has no emulator
        // default, so a missing extra + a null persisted value leaves it null (legacy
        // same-port scheme-swap fallback).
        if (urlExtra == null || tokenExtra == null || gatewayPortExtra == null) {
            val cfg = runBlocking { ServerConfig(filesDir).read() }
            if (urlExtra == null) cfg.serverUrl.ifEmpty { null }?.let { serverUrl = it }
            if (tokenExtra == null) cfg.token.ifEmpty { null }?.let { token = it }
            if (gatewayPortExtra == null) cfg.gatewayPort?.let { gatewayPort = it }
        }
        startForeground(1, buildNotification())

        val gen = ++generation
        // Re-provision: if we're already running (a re-delivery with new
        // server_url/token), tear down the prior session/sensor/socket before
        // re-initializing. The per-call listeners below capture `gen`, so the
        // async onDisconnected/onClosed fired by the OLD sensor's/socket's
        // teardown (on the GATT/WS thread, after these calls return) are
        // ignored — they see a stale generation. NOT stopSelf: the service
        // keeps running with the new config.
        if (::session.isInitialized) {
            runCatching { session.stop().forEach(::execute) }
            socket?.close(); socket = null
            if (::sensor.isInitialized) sensor.stop()
        }

        session = RelaySession(sessionId = UUID.randomUUID().toString())
        sensor = SensorLink(this, sensorListener(gen))
        // Publish to RelayController: scanning for the device. The
        // device state machine in DeviceState captures this; the
        // next update is onConnected (Connected) or onDisconnected
        // (Disconnected) below.
        RelayController.updateDevice(DeviceState.Scanning)
        RelayController.updateConnection(RelayConnectionState.BleScanning)
        sensor.start()
        return START_STICKY
    }

    // --- device (BLE) events ---
    private fun sensorListener(gen: Int) = object : SensorLink.Listener {
        private fun stale() = gen != generation
        override fun onConnected() {
            if (stale()) return
            // Derive the WS URL from the provisioned HTTP host + the server-
            // reported gateway port. The HTTP API and the WS gateway are on
            // separate ports; reusing the HTTP port (scheme-swap only) makes
            // the upgrade hit the HTTP server → "Expected HTTP 101 response".
            val wsUrl = wsGatewayUrl(serverUrl, gatewayPort)
            Log.i(TAG, "device connected; opening socket to $wsUrl")
            // Publish to RelayController: device up + BLE link up.
            // Name is null until Phase 4 reads the device's advertised
            // name from a separate characteristic read.
            val addr = sensor.deviceAddress()
            RelayController.updateDevice(DeviceState.Connected(addr ?: "", name = null))
            RelayController.updateConnection(RelayConnectionState.BleConnected(addr ?: ""))
            socket = ServerSocket(wsUrl, token, socketListener(gen)).also { it.connect() }
        }
        override fun onAudio(packet: ByteArray) {
            if (stale()) return
            execute(session.onDeviceAudio(packet))
        }
        override fun onCommandAck(payload: ByteArray) {
            if (stale()) return
            execute(session.onDeviceCommandAck(payload))
        }
        override fun onDisconnected(reason: String) {
            if (stale()) return
            Log.w(TAG, "device disconnected: $reason"); teardown()
            // Publish to RelayController: device down + connection idle.
            RelayController.updateDevice(DeviceState.Disconnected(reason))
            RelayController.updateConnection(RelayConnectionState.Idle)
        }
    }

    // --- server (WebSocket) events ---
    private fun socketListener(gen: Int) = object : ServerSocket.Listener {
        private fun stale() = gen != generation
        override fun onOpen() {
            if (stale()) return
            Log.i(TAG, "socket open; sending hello")
            // Publish to RelayController: server authenticated + relay
            // is Live. `sinceMs` is 0 on first connect; later phases
            // will track elapsed time and re-publish.
            RelayController.updateServer(ServerState.Authenticated)
            RelayController.updateConnection(
                RelayConnectionState.Live(sessionId = session.sessionId, sinceMs = 0L)
            )
            session.start().forEach(::execute)
        }
        override fun onText(text: String) {
            if (stale()) return
            session.onServerMessage(text).forEach(::execute)
        }
        override fun onClosed(reason: String) {
            if (stale()) return
            Log.w(TAG, "socket closed: $reason"); teardown()
            // Publish to RelayController: socket failed + server unreachable.
            RelayController.updateConnection(RelayConnectionState.Failed(reason))
            RelayController.updateServer(ServerState.Unreachable(reason))
        }
    }

    private fun execute(action: RelayAction) {
        when (action) {
            is RelayAction.SendServerBinary -> socket?.sendBinary(action.data)
            is RelayAction.SendServerText -> socket?.sendText(action.text)
            is RelayAction.WriteDeviceCommand -> sensor.writeCommand(action.frame)
            is RelayAction.Note -> Log.i(TAG, action.message)
        }
    }

    private fun teardown() {
        runCatching { session.stop().forEach(::execute) }
        socket?.close(); socket = null
        sensor.stop()
        stopSelf()
    }

    override fun onDestroy() {
        socket?.close()
        if (::sensor.isInitialized) sensor.stop()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun buildNotification(): Notification {
        val mgr = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (mgr.getNotificationChannel(CHANNEL) == null) {
            mgr.createNotificationChannel(
                NotificationChannel(CHANNEL, "Sense Relay", NotificationManager.IMPORTANCE_LOW),
            )
        }
        return Notification.Builder(this, CHANNEL)
            .setContentTitle("Sense Relay")
            .setContentText("Bridging wearable ↔ server")
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setOngoing(true)
            .build()
    }
}