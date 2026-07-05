package com.sense.relay.relay

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * The architectural keystone of Phase 2: the **only** place in the
 * app that holds relay/connection state. Every UI that needs to
 * know what's going on (Home, Device, Recordings) subscribes to
 * [state]; the only writer is [com.sense.relay.RelayService], which
 * calls the `updateX` methods.
 *
 * The controller is a Kotlin `object` (process-singleton) by
 * design: there is exactly one relay per process, and the
 * "single-writer" rule is what keeps the StateFlow's emissions
 * monotonic and consistent. The `_state` field is intentionally
 * `private` so no caller can reach in and mutate the StateFlow
 * outside the `updateX`/`requestRefresh` API — the
 * `asStateFlow()` projection is the only thing that escapes.
 *
 * **Test concurrency note:** Gradle's default test fork model
 * runs every test class in its own JVM, but multiple test classes
 * *within* a single fork share static state. Because this repo
 * runs all unit tests in a single fork (the default), the
 * singleton carries state across tests. The [RelayControllerTest]
 * resets state inline at the top of each test for that reason —
 * see `reset()`. Tests that need isolation can inject their own
 * `MutableStateFlow<RelayState>` via a test seam (none required
 * for the brief's tests).
 */
object RelayController {

    private val _state = MutableStateFlow(RelayState.Initial)

    /** Read-only stream of the current composite [RelayState].
     *  Subscribers see the latest value on subscribe; subsequent
     *  updates are delivered as the service writes. */
    val state: StateFlow<RelayState> = _state.asStateFlow()

    /** Replace the [RelayState.device] field. Other fields preserved. */
    fun updateDevice(d: DeviceState) {
        _state.value = _state.value.copy(device = d)
    }

    /** Replace the [RelayState.connection] field. Other fields preserved. */
    fun updateConnection(c: RelayConnectionState) {
        _state.value = _state.value.copy(connection = c)
    }

    /** Replace the [RelayState.server] field. Other fields preserved. */
    fun updateServer(s: ServerState) {
        _state.value = _state.value.copy(server = s)
    }

    /** Set or clear the soft [RelayState.lastError] slot. */
    fun setError(msg: String?) {
        _state.value = _state.value.copy(lastError = msg)
    }

    /**
     * Force a re-emission of the current state. Phase 7's
     * re-provisioning flow uses this to wake collectors that may
     * have stopped seeing new values because the `updateX` calls
     * settled on an equivalent instance (e.g. a no-op transition).
     * The new instance is `data class`-equal to the old one but
     * is a fresh `RelayState` object, so `StateFlow`'s
     * "value-changed" filter re-fires.
     */
    fun requestRefresh() {
        _state.value = _state.value.copy()
    }
}
