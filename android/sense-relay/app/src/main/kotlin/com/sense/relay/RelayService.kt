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

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val urlExtra = intent?.getStringExtra("server_url")
        val tokenExtra = intent?.getStringExtra("token")
        if (urlExtra != null) serverUrl = urlExtra
        if (tokenExtra != null) token = tokenExtra
        // START_STICKY redelivery: intent extras are null — restore both from the persisted
        // config (mirroring the token fallback). Only adopt a non-empty persisted value so we
        // never overwrite a valid default with an empty one.
        if (urlExtra == null || tokenExtra == null) {
            val cfg = runBlocking { ServerConfig(filesDir).read() }
            if (urlExtra == null) cfg.serverUrl.ifEmpty { null }?.let { serverUrl = it }
            if (tokenExtra == null) cfg.token.ifEmpty { null }?.let { token = it }
        }
        startForeground(1, buildNotification())

        session = RelaySession(sessionId = UUID.randomUUID().toString())
        sensor = SensorLink(this, sensorListener)
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
    private val sensorListener = object : SensorLink.Listener {
        override fun onConnected() {
            val wsUrl = serverUrl.replaceFirst(Regex("^https?://"),
                if (serverUrl.startsWith("https")) "wss://" else "ws://")
            Log.i(TAG, "device connected; opening socket to $wsUrl")
            // Publish to RelayController: device up + BLE link up.
            // Name is null until Phase 4 reads the device's advertised
            // name from a separate characteristic read.
            val addr = sensor.deviceAddress()
            RelayController.updateDevice(DeviceState.Connected(addr ?: "", name = null))
            RelayController.updateConnection(RelayConnectionState.BleConnected(addr ?: ""))
            socket = ServerSocket(wsUrl, token, socketListener).also { it.connect() }
        }
        override fun onAudio(packet: ByteArray) = execute(session.onDeviceAudio(packet))
        override fun onCommandAck(payload: ByteArray) = execute(session.onDeviceCommandAck(payload))
        override fun onDisconnected(reason: String) {
            Log.w(TAG, "device disconnected: $reason"); teardown()
            // Publish to RelayController: device down + connection idle.
            RelayController.updateDevice(DeviceState.Disconnected(reason))
            RelayController.updateConnection(RelayConnectionState.Idle)
        }
    }

    // --- server (WebSocket) events ---
    private val socketListener = object : ServerSocket.Listener {
        override fun onOpen() {
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
        override fun onText(text: String) = session.onServerMessage(text).forEach(::execute)
        override fun onClosed(reason: String) {
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
