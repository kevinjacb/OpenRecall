package com.sense.relay.relay

import kotlinx.coroutines.test.runTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertIs
import kotlin.test.assertNotSame

/**
 * Pins the contract of [RelayController]:
 *   - Initial state is [RelayState.Initial].
 *   - Every `updateX` produces a new [RelayState] with the new field;
 *     the other fields are preserved.
 *   - [setError] updates the `lastError` slot; passing null clears it.
 *   - [requestRefresh] preserves the state value (the *content* of
 *     `state.value` is unchanged; Phase 7's re-provisioning flow
 *     uses a separate tick channel for wakeups).
 *   - [state] is read-only outside the controller: a caller cannot
 *     cast it back to a MutableStateFlow and mutate it.
 *
 * The controller is a Kotlin `object` (process-singleton). Tests
 * run single-threaded in this repo, so the singleton is safe across
 * tests as long as each test is self-contained — they all start by
 * resetting to Initial. No test depends on a previous test's state.
 */
class RelayControllerTest {

    // Each test resets the controller to Initial. A beforeEach
    // would be cleaner but a Kotlin `object` has no constructor to
    // hook, so we restore the state inline at the top of each test.
    private fun reset() {
        RelayController.updateDevice(DeviceState.Unknown)
        RelayController.updateConnection(RelayConnectionState.Idle)
        RelayController.updateServer(ServerState.Unknown)
        RelayController.setError(null)
    }

    @Test fun initialStateIsRelayStateInitial() {
        reset()
        assertEquals(RelayState.Initial, RelayController.state.value)
    }

    @Test fun updateDeviceReplacesDeviceFieldOnly() {
        reset()
        val before = RelayController.state.value
        RelayController.updateDevice(DeviceState.Connected("AA:BB", "sense-1"))
        val after = RelayController.state.value
        assertNotSame(before, after) // new instance
        assertEquals(DeviceState.Connected("AA:BB", "sense-1"), after.device)
        assertEquals(before.connection, after.connection) // preserved
        assertEquals(before.server, after.server)
        assertEquals(before.lastError, after.lastError)
    }

    @Test fun updateConnectionReplacesConnectionFieldOnly() {
        reset()
        val before = RelayController.state.value
        RelayController.updateConnection(RelayConnectionState.BleScanning)
        val after = RelayController.state.value
        assertNotSame(before, after)
        assertEquals(RelayConnectionState.BleScanning, after.connection)
        assertEquals(before.device, after.device)
        assertEquals(before.server, after.server)
        assertEquals(before.lastError, after.lastError)
    }

    @Test fun updateServerReplacesServerFieldOnly() {
        reset()
        val before = RelayController.state.value
        RelayController.updateServer(ServerState.Authenticated)
        val after = RelayController.state.value
        assertNotSame(before, after)
        assertEquals(ServerState.Authenticated, after.server)
        assertEquals(before.device, after.device)
        assertEquals(before.connection, after.connection)
        assertEquals(before.lastError, after.lastError)
    }

    @Test fun setErrorSetsLastErrorField() {
        reset()
        RelayController.setError("boom")
        assertEquals("boom", RelayController.state.value.lastError)
    }

    @Test fun setErrorNullClearsLastError() {
        reset()
        RelayController.setError("boom")
        RelayController.setError(null)
        assertEquals(null, RelayController.state.value.lastError)
    }

    @Test fun requestRefreshLeavesStateValueUnchanged() {
        // requestRefresh's job is to wake collectors that may have
        // stopped seeing new values; the *content* of `state.value`
        // is preserved. Collectors use a separate tick channel
        // (Phase 7) to re-evaluate. Pin the simple invariant here:
        // the state value is unchanged.
        reset()
        val before = RelayController.state.value
        RelayController.requestRefresh()
        val after = RelayController.state.value
        assertEquals(before, after)
    }

    @Test fun publicStateIsAStateFlowNotAMutableStateFlow() {
        // The public field's declared type is `StateFlow<RelayState>`,
        // not `MutableStateFlow<RelayState>`. The Kotlin compiler
        // enforces that at the API boundary: a caller cannot write
        // `controller.state.value = ...` because `StateFlow` has no
        // setter. We assert the same constraint at runtime by
        // attempting the downcast — a `ClassCastException` confirms
        // the field's declared type is the read-only `StateFlow`.
        reset()
        val s = RelayController.state
        // Reading .value is fine.
        val current: RelayState = s.value
        assertEquals(RelayState.Initial, current)
        // The downcast fails (StateFlow has no `value =`):
        assertFailsWith<ClassCastException> {
            @Suppress("UNCHECKED_CAST")
            (s as kotlinx.coroutines.flow.MutableStateFlow<RelayState>)
        }
    }

    @Test fun fullUpdateCycle() = runTest {
        reset()
        // Simulate the relay's lifecycle in miniature.
        RelayController.updateDevice(DeviceState.Scanning)
        RelayController.updateConnection(RelayConnectionState.BleScanning)
        assertEquals(DeviceState.Scanning, RelayController.state.value.device)
        assertEquals(RelayConnectionState.BleScanning, RelayController.state.value.connection)

        RelayController.updateDevice(DeviceState.Connected("AA:BB", null))
        RelayController.updateConnection(RelayConnectionState.BleConnected("AA:BB"))
        assertEquals(DeviceState.Connected("AA:BB", null), RelayController.state.value.device)
        assertEquals(RelayConnectionState.BleConnected("AA:BB"), RelayController.state.value.connection)

        RelayController.updateConnection(RelayConnectionState.SocketConnecting("host:8765"))
        RelayController.updateServer(ServerState.Authenticated)
        RelayController.updateConnection(RelayConnectionState.Live("sess-1", 0L))
        val final = RelayController.state.value
        assertIs<RelayConnectionState.Live>(final.connection)
        assertEquals("sess-1", final.connection.sessionId)
        assertEquals(ServerState.Authenticated, final.server)
        assertEquals(DeviceState.Connected("AA:BB", null), final.device)
    }

    @Test fun updatesAreIndependent() {
        // Each `updateX` method only touches its own field. Set
        // every field to a non-default value, then mutate one and
        // verify the others are unchanged.
        reset()
        RelayController.updateDevice(DeviceState.Connected("X", null))
        RelayController.updateConnection(RelayConnectionState.Live("s1", 0))
        RelayController.updateServer(ServerState.Reachable)
        RelayController.setError("e1")
        val before = RelayController.state.value

        // Mutate just connection.
        RelayController.updateConnection(RelayConnectionState.Failed("oops"))
        val after = RelayController.state.value

        assertEquals(before.device, after.device)
        assertEquals(before.server, after.server)
        assertEquals(before.lastError, after.lastError)
        assertEquals(RelayConnectionState.Failed("oops"), after.connection)
    }
}
