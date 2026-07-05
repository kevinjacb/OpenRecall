package com.sense.relay.relay

/**
 * Where the wearable is in the BLE lifecycle, as observed by
 * [RelayController]. Sealed so the UI can exhaustively render the
 * state with a single `when`. The data is intentionally minimal:
 * the controller is the source of truth; everything else derives
 * from it.
 */
sealed interface DeviceState {
    /** We haven't tried to find the device yet. */
    data object Unknown : DeviceState

    /** BLE link is up. `address` is the MAC/BLE address; `name` is
     *  the advertised device name (null if the device didn't expose
     *  one, which is rare on the firmware we ship). */
    data class Connected(val address: String, val name: String?) : DeviceState

    /** BLE scan is in flight; the device may or may not appear. */
    data object Scanning : DeviceState

    /** BLE link is down. `reason` is a short human-readable why
     *  (timeout, gatt state 0, lost connection, etc.). The UI may
     *  surface it verbatim or treat it as a debug-only string. */
    data class Disconnected(val reason: String) : DeviceState
}
