package com.sense.relay.relay

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.runTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertIs
import kotlin.test.assertNotSame
import kotlin.test.assertTrue

/**
 * Pins the contract of [RelayController]:
 *   - Initial state is [RelayState.Initial].
 *   - Every `updateX` produces a new [RelayState] with the new field;
 *     the other fields are preserved.
 *   - [setError] updates the `lastError` slot; passing null clears it.
 *   - [requestRefresh] bumps [RelayState.revision] and re-emits the
 *     state — collectors observe a new value even when the headline
 *     fields are unchanged. This is the "wakeup" mechanism Phase 7's
 *     re-provisioning flow depends on.
 *   - [state] is read-only outside the controller: a caller cannot
 *     cast it back to a MutableStateFlow and mutate it.
 *
 * The controller is a Kotlin `object` (process-singleton). Tests
 * run single-threaded in this repo, so the singleton is safe across
 * tests as long as each test is self-contained — they all start by
 * resetting to Initial. No test depends on a previous test's state.
 */
class RelayControllerTest {

    // Each test resets the controller to Initial. The
    // `RelayController.reset()` method restores both the headline
    // fields AND the revision counter (the per-field `updateX`
    // methods don't touch revision, so a test that calls them to
    // "reset" would leave the revision at whatever value the
    // previous test bumped it to).
    private fun reset() {
        RelayController.reset()
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

    @Test fun requestRefreshReEmitsCurrentState() = runBlocking {
        // requestRefresh is the controller's "wakeup" mechanism. It
        // must produce a fresh emission on the public `state` flow
        // even when the headline fields (device / connection /
        // server / lastError) are unchanged, so that a StateFlow
        // collector downstream sees the new value. The
        // implementation does this by bumping `revision`.
        //
        // We exercise the actual contract: a StateFlow collector
        // must receive a new value after requestRefresh() is called.
        // We also assert the headline fields are preserved (this
        // is a *re-emit*, not a state change) and that `revision`
        // is monotonic.
        reset()

        // Set a non-default state so we can verify the headline
        // fields survive the refresh.
        RelayController.updateDevice(DeviceState.Connected("AA:BB", "sense-1"))
        RelayController.updateConnection(RelayConnectionState.Live("sess-1", 0L))
        RelayController.updateServer(ServerState.Authenticated)
        RelayController.setError("keep")

        // Only `requestRefresh` bumps the revision; the per-field
        // `updateX` calls don't. After 4 updateX calls + a
        // starting revision of 0, we expect revision == 0.
        val beforeRevision = RelayController.state.value.revision
        assertEquals(0, beforeRevision)

        // Drive a collector on a real dispatcher. `StateFlow.collect`
        // uses an internal `select` clause that does not cooperate
        // with `TestDispatcher` in every coroutines version, so we
        // run the collector on `Dispatchers.Default` and use a
        // small real-time wait for propagation.
        val emissions = mutableListOf<RelayState>()
        val collectorJob = launch(Dispatchers.Default) {
            RelayController.state.collect { emissions.add(it) }
        }
        try {
            // Give the collector a moment to subscribe and receive
            // the current value.
            delay(50)
            val beforeRefreshEmissionCount = emissions.size
            assertTrue(
                beforeRefreshEmissionCount >= 1,
                "expected the collector to have received at least the initial value; got $beforeRefreshEmissionCount"
            )

            // Trigger the refresh. This must produce a new emission
            // because the revision changes.
            RelayController.requestRefresh()

            // Wait for the new emission to propagate (real-time).
            val deadline = System.currentTimeMillis() + 2000
            while (emissions.size == beforeRefreshEmissionCount && System.currentTimeMillis() < deadline) {
                delay(10)
            }
            assertTrue(
                emissions.size > beforeRefreshEmissionCount,
                "expected a new emission after requestRefresh; size still $beforeRefreshEmissionCount"
            )

            // The direct value check: revision was bumped.
            val after = RelayController.state.value
            assertEquals(beforeRevision + 1, after.revision)
            // The headline fields are unchanged.
            assertEquals(DeviceState.Connected("AA:BB", "sense-1"), after.device)
            assertEquals(RelayConnectionState.Live("sess-1", 0L), after.connection)
            assertEquals(ServerState.Authenticated, after.server)
            assertEquals("keep", after.lastError)

            // The collector must have seen a new emission whose
            // revision is exactly one more than the pre-refresh
            // emission. `emissions.last()` is the most recent, and
            // its revision is `beforeRevision + 1`.
            val post = emissions.last()
            assertEquals(beforeRevision + 1, post.revision)
            assertEquals(DeviceState.Connected("AA:BB", "sense-1"), post.device)
            assertEquals(RelayConnectionState.Live("sess-1", 0L), post.connection)
            assertEquals(ServerState.Authenticated, post.server)
            assertEquals("keep", post.lastError)

            // A second refresh bumps again.
            val beforeSecond = emissions.size
            RelayController.requestRefresh()
            val deadline2 = System.currentTimeMillis() + 2000
            while (emissions.size == beforeSecond && System.currentTimeMillis() < deadline2) {
                delay(10)
            }
            assertEquals(beforeRevision + 2, RelayController.state.value.revision)
            assertEquals(beforeRevision + 2, emissions.last().revision)
        } finally {
            collectorJob.cancel()
        }
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
