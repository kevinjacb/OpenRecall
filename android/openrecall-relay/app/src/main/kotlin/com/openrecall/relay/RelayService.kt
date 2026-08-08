package com.openrecall.relay

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.IBinder
import android.util.Log
import com.openrecall.relay.ble.SensorLink
import com.openrecall.relay.data.RepositoryModule
import com.openrecall.relay.net.ServerSocket
import com.openrecall.relay.net.wsGatewayUrl
import com.openrecall.relay.relay.Backoff
import com.openrecall.relay.relay.DeviceState
import com.openrecall.relay.relay.RelayConnectionState
import com.openrecall.relay.relay.RelayController
import com.openrecall.relay.relay.ServerState
import com.openrecall.relay.store.ServerConfig
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
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
 * otherwise call [softDrop] on the NEW session.
 *
 * **Auto-reconnect (P-refresh):** a transient BLE/WS drop no longer `stopSelf`s
 * the service. [softDrop] stops the current link and [scheduleReconnect]
 * publishes [RelayConnectionState.Reconnecting], then re-runs the scan path
 * after an exponential [Backoff] (1s→2s→4s→8s→30s, ~6 attempts). A successful
 * reconnect resets the backoff counter; after the cap, the service publishes
 * [RelayConnectionState.Failed] and stops retrying — the manual "Retry
 * connection" button (which re-launches this service via [com.openrecall.relay.relay.RelayStarter])
 * is the escape hatch. Re-provision during backoff cancels the retry (the
 * stale generation guard).
 */
class RelayService : Service() {

    private companion object {
        const val TAG = "RelayService"
        const val CHANNEL = "openrecall_relay"
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

    /**
     * Auto-reconnect attempt counter for the current [generation]. Reset to 0
     * on every `onStartCommand` (a fresh provisioning run starts the backoff
     * schedule over). Incremented each time [scheduleReconnect] fires a retry.
     */
    @Volatile
    private var reconnectAttempt = 0

    /** The in-flight reconnect coroutine, if any. Cancelled before scheduling a
     *  new one (a second drop while a retry is pending supersedes it) and on
     *  re-provision / destroy. */
    private var reconnectJob: Job? = null

    /** Scope for reconnect coroutines. A [SupervisorJob] so one failed retry
     *  doesn't cancel siblings; cancelled in [onDestroy]. */
    private val serviceScope = CoroutineScope(SupervisorJob())

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
        // A fresh provisioning run cancels any in-flight auto-reconnect and
        // resets the backoff schedule.
        reconnectJob?.cancel(); reconnectJob = null
        reconnectAttempt = 0
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

        session = RelaySession(
            sessionId = UUID.randomUUID().toString(),
            speakerCache = RepositoryModule.repos.speakerCache,
        )
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
            // onDeviceAudio holds audio until `hello` has been sent (see
            // RelaySession), so this is safe to call before the socket's
            // onOpen fires — it returns no actions until start() flushes.
            session.onDeviceAudio(packet).forEach(::execute)
        }
        override fun onCommandAck(payload: ByteArray) {
            if (stale()) return
            execute(session.onDeviceCommandAck(payload))
        }
        override fun onDisconnected(reason: String) {
            if (stale()) return
            Log.w(TAG, "device disconnected: $reason")
            // Publish to RelayController: device down. The connection state
            // is set by [softDrop] (Reconnecting, or Failed once the backoff
            // cap is hit) — NOT Idle, so the UI offers "Retry connection"
            // while auto-reconnect is in progress.
            RelayController.updateDevice(DeviceState.Disconnected(reason))
            softDrop(gen, reason)
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
            // A successful reconnect resets the backoff schedule so the next
            // drop starts the retry count over.
            reconnectAttempt = 0
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
            Log.w(TAG, "socket closed: $reason")
            // Publish to RelayController: server unreachable; [softDrop] sets
            // the connection state (Reconnecting / Failed).
            RelayController.updateServer(ServerState.Unreachable(reason))
            softDrop(gen, reason)
        }
    }

    private fun execute(action: RelayAction) {
        when (action) {
            is RelayAction.SendServerBinary -> socket?.sendBinary(action.data)
            is RelayAction.SendServerText -> socket?.sendText(action.text)
            is RelayAction.WriteDeviceCommand -> sensor.writeCommand(action.frame)
            // P3: server-initiated proactive answer. Append to the
            // process-singleton chat history; the chat screen picks
            // it up via its StateFlow observer.
            is RelayAction.ForwardToChatHistory ->
                RepositoryModule.repos.chatHistoryStore.append(action.message)
            is RelayAction.Note -> Log.i(TAG, action.message)
        }
    }

    /**
     * Transient drop (BLE or WS): stop the current link WITHOUT `stopSelf`, then
     * schedule an auto-reconnect with backoff. The service stays alive so the
     * retry can re-scan / re-open the socket. Generation-guarded via [gen] — a
     * re-provision during the drop cancels the retry (the stale listener's
     * `softDrop` is a no-op once [generation] has moved on).
     *
     * After the backoff cap ([Backoff.MAX_ATTEMPTS]) is hit, [scheduleReconnect]
     * publishes a terminal [RelayConnectionState.Failed] and stops retrying —
     * the manual "Retry connection" button (which re-launches this service) is
     * the escape hatch.
     */
    private fun softDrop(gen: Int, reason: String) {
        if (gen != generation) return
        // Stop the current link so the retry starts clean. session.stop()
        // flushes any pending server-bound frames; sensor.stop() cancels the
        // BLE scan/GATT. NOT stopSelf — the service must outlive the drop.
        runCatching { if (::session.isInitialized) session.stop().forEach(::execute) }
        socket?.close(); socket = null
        if (::sensor.isInitialized) runCatching { sensor.stop() }
        scheduleReconnect(gen, reason)
    }

    /**
     * Publish [Reconnecting] and, after the backoff delay, re-run the scan
     * path (fresh sensor + `start()`). [onConnected] re-opens the socket; a
     * further drop re-enters [softDrop] with an incremented attempt. After
     * [Backoff.MAX_ATTEMPTS], publish [Failed] and stop retrying.
     */
    private fun scheduleReconnect(gen: Int, reason: String) {
        if (gen != generation) return
        if (Backoff.shouldGiveUp(reconnectAttempt)) {
            Log.w(TAG, "reconnect gave up after $reconnectAttempt attempts: $reason")
            RelayController.updateConnection(RelayConnectionState.Failed(reason))
            return
        }
        val delayMs = Backoff.nextBackoffMs(reconnectAttempt)
        Log.i(TAG, "reconnect attempt $reconnectAttempt in ${delayMs}ms")
        RelayController.updateConnection(RelayConnectionState.Reconnecting(delayMs))
        val attempt = reconnectAttempt
        reconnectAttempt++
        // A second drop while a retry is pending supersedes it.
        reconnectJob?.cancel()
        reconnectJob = serviceScope.launch {
            delay(delayMs)
            if (gen != generation) return@launch  // re-provisioned mid-backoff
            // Re-run the scan path with the SAME generation's listener, so the
            // new sensor's callbacks stay non-stale (and share the backoff
            // counter). onConnected re-opens the socket; a further drop
            // re-enters softDrop with `attempt+1`.
            sensor = SensorLink(this@RelayService, sensorListener(gen))
            RelayController.updateDevice(DeviceState.Scanning)
            RelayController.updateConnection(RelayConnectionState.BleScanning)
            sensor.start()
            Log.i(TAG, "reconnect attempt $attempt started (scanning)")
        }
    }

    override fun onDestroy() {
        reconnectJob?.cancel()
        serviceScope.cancel()
        socket?.close()
        if (::sensor.isInitialized) sensor.stop()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun buildNotification(): Notification {
        val mgr = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (mgr.getNotificationChannel(CHANNEL) == null) {
            mgr.createNotificationChannel(
                NotificationChannel(CHANNEL, "OpenRecall Relay", NotificationManager.IMPORTANCE_LOW),
            )
        }
        return Notification.Builder(this, CHANNEL)
            .setContentTitle("OpenRecall Relay")
            .setContentText("Bridging wearable ↔ server")
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setOngoing(true)
            .build()
    }
}