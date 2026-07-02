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

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        intent?.getStringExtra("server_url")?.let { serverUrl = it }
        startForeground(1, buildNotification())

        session = RelaySession(sessionId = UUID.randomUUID().toString())
        sensor = SensorLink(this, sensorListener)
        sensor.start()
        return START_STICKY
    }

    // --- device (BLE) events ---
    private val sensorListener = object : SensorLink.Listener {
        override fun onConnected() {
            Log.i(TAG, "device connected; opening socket to $serverUrl")
            socket = ServerSocket(serverUrl, socketListener).also { it.connect() }
        }
        override fun onAudio(packet: ByteArray) = execute(session.onDeviceAudio(packet))
        override fun onCommandAck(payload: ByteArray) = execute(session.onDeviceCommandAck(payload))
        override fun onDisconnected(reason: String) {
            Log.w(TAG, "device disconnected: $reason"); teardown()
        }
    }

    // --- server (WebSocket) events ---
    private val socketListener = object : ServerSocket.Listener {
        override fun onOpen() {
            Log.i(TAG, "socket open; sending hello")
            session.start().forEach(::execute)
        }
        override fun onText(text: String) = session.onServerMessage(text).forEach(::execute)
        override fun onClosed(reason: String) {
            Log.w(TAG, "socket closed: $reason"); teardown()
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
