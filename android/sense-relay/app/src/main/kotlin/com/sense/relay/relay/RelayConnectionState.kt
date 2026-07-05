package com.sense.relay.relay

/**
 * Where the relay is in its full lifecycle (BLE + WebSocket). Sealed
 * so the UI can exhaustively render with one `when`. The transitions
 * are driven by [RelayService] through [RelayController] — that's
 * the only writer.
 *
 * The names line up with user-visible states the UI cares about:
 *   - Idle: not started.
 *   - BleScanning: looking for the wearable.
 *   - BleConnected: device up, socket not yet.
 *   - SocketConnecting: opening the WS to the server.
 *   - Live: session is open, frames are flowing.
 *   - Reconnecting: socket dropped, retrying (backoff handled by
 *     the service; the controller just records the intent).
 *   - Failed: terminal — user must take action.
 */
sealed interface RelayConnectionState {
    data object Idle : RelayConnectionState
    data object BleScanning : RelayConnectionState

    /** BLE link is up at the given address. */
    data class BleConnected(val address: String) : RelayConnectionState

    /** Opening a WebSocket to the given host (host:port form). */
    data class SocketConnecting(val host: String) : RelayConnectionState

    /** Session is open. `sessionId` is the server-issued id; `sinceMs`
     *  is the elapsed-since-start ms used for live-time display. */
    data class Live(val sessionId: String, val sinceMs: Long) : RelayConnectionState

    /** Socket dropped; the service is retrying after `afterMs` of
     *  backoff. The DeviceState portion of [RelayState] is unchanged. */
    data class Reconnecting(val afterMs: Long) : RelayConnectionState

    /** Terminal failure. `reason` is a short why (auth refused,
     *  server gone, etc.). */
    data class Failed(val reason: String) : RelayConnectionState
}
