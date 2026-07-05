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
     * Force a re-emission of the current state by bumping
     * [RelayState.revision]. The other fields are preserved.
     *
     * Phase 7's re-provisioning flow uses this to wake collectors
     * that may have stopped seeing new values because the `updateX`
     * calls settled on an equivalent instance (e.g. a no-op
     * transition or a "user just pulled to refresh" gesture that
     * doesn't change the headline state).
     *
     * Note on the mechanism: a naive `_state.value = _state.value.copy()`
     * does NOT re-emit, because `MutableStateFlow.value` setter
     * drops values that are `equals`-equal to the current value
     * (and `data class.copy()` with no args produces an equals-equal
     * instance). To force the re-emission, this method bumps the
     * [RelayState.revision] counter; the new `RelayState` is
     * `!=` the old one (revision differs), so the `StateFlow`
     * emits. Consumers that care about the headline state should
     * compare the data fields; consumers that want to react to
     * the refresh itself should key on `revision`.
     */
    fun requestRefresh() {
        _state.value = _state.value.copy(revision = _state.value.revision + 1)
    }

    /**
     * Reset the controller to [RelayState.Initial]. Intended for
     * tests (the controller is a process-singleton; tests need a
     * way to clear state between runs). The headline fields are
     * restored AND the `revision` counter is zeroed.
     *
     * Production callers should never invoke this — the controller
     * is meant to be written only through `updateX`/`requestRefresh`.
     * The method is intentionally public so test code in any
     * package can call it.
     */
    fun reset() {
        _state.value = RelayState.Initial
    }
}
