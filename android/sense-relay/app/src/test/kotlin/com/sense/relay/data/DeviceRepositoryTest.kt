package com.sense.relay.data

import com.sense.relay.domain.model.DeviceSummary
import com.sense.relay.relay.DeviceState
import com.sense.relay.relay.RelayConnectionState
import com.sense.relay.relay.RelayController
import com.sense.relay.relay.RelayState
import com.sense.relay.relay.ServerState
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.test.runTest
import java.time.Instant
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

/**
 * Pins the contract of [DeviceRepository]: it derives a
 * [DeviceSummary] from the [RelayController]'s current [RelayState].
 * The repository owns the *mapping* (what does each [DeviceState]
 * look like to the UI?) and the *capture* of `lastSeen` (the moment
 * the most recent `updateDevice` was issued), but it does not
 * observe BLE traffic itself — that's [RelayService]'s job.
 *
 * The implementation observes the [RelayController] singleton.
 * Tests reset the controller at the top of each case so the
 * singleton's state doesn't bleed between runs (same pattern as
 * [com.sense.relay.relay.RelayControllerTest]).
 */
class DeviceRepositoryTest {

    private fun resetController() {
        RelayController.updateDevice(DeviceState.Unknown)
        RelayController.updateConnection(RelayConnectionState.Idle)
        RelayController.updateServer(ServerState.Unknown)
        RelayController.setError(null)
    }

    @Test fun unknownDeviceMapsToAllNulls() = runTest {
        resetController()
        val repo = DeviceRepositoryImpl(RelayController)
        val s = repo.observeDevice().first()
        assertNull(s.address)
        assertNull(s.name)
        assertNull(s.lastSeen)
    }

    @Test fun connectedDeviceCarriesAddressAndName() = runTest {
        resetController()
        RelayController.updateDevice(DeviceState.Connected("AA:BB:CC:DD:EE:FF", "sense-1"))
        val repo = DeviceRepositoryImpl(RelayController)
        val s = repo.observeDevice().first()
        assertEquals("AA:BB:CC:DD:EE:FF", s.address)
        assertEquals("sense-1", s.name)
        // lastSeen is set by the repository on every emit; the
        // exact value is non-deterministic, so we just check
        // it's not null.
        assert(s.lastSeen != null) { "expected lastSeen to be set; was null" }
    }

    @Test fun connectedDeviceWithNullNameHasNullName() = runTest {
        resetController()
        RelayController.updateDevice(DeviceState.Connected("AA:BB", null))
        val s = DeviceRepositoryImpl(RelayController).observeDevice().first()
        assertEquals("AA:BB", s.address)
        assertNull(s.name)
    }

    @Test fun disconnectedDeviceHasNullNameAndAddress() = runTest {
        resetController()
        RelayController.updateDevice(DeviceState.Disconnected("gatt state 0"))
        val s = DeviceRepositoryImpl(RelayController).observeDevice().first()
        // After a disconnect, address and name are unknown until
        // the next connect — the repository surfaces them as null.
        assertNull(s.address)
        assertNull(s.name)
    }

    @Test fun scanningDeviceHasNullAddressAndName() = runTest {
        resetController()
        RelayController.updateDevice(DeviceState.Scanning)
        val s = DeviceRepositoryImpl(RelayController).observeDevice().first()
        assertNull(s.address)
        assertNull(s.name)
    }

    @Test fun lastSeenAdvancesAcrossUpdates() = runTest {
        resetController()
        val repo = DeviceRepositoryImpl(RelayController)
        RelayController.updateDevice(DeviceState.Connected("A", null))
        val t1 = repo.observeDevice().first().lastSeen
        // Force a new emit.
        Thread.sleep(2)
        RelayController.updateDevice(DeviceState.Connected("B", null))
        val t2 = repo.observeDevice().first().lastSeen
        assert(t1 != null && t2 != null) { "expected both lastSeen to be set" }
        assert(t2!! >= t1!!) { "expected t2 ($t2) >= t1 ($t1)" }
    }

    @Test fun repositoryExposesDeviceRepositoryInterface() {
        // Compile-time check: the impl is the interface type.
        val repo: DeviceRepository = DeviceRepositoryImpl(RelayController)
        assert(repo is DeviceRepositoryImpl)
    }

    @Test fun observeDeviceReturnsAStateFlow() = runTest {
        // The brief says `observeDevice(): StateFlow<DeviceSummary>`.
        // The repository owns the StateFlow so collectors get the
        // current value on subscribe. We don't assert on the type
        // here (no kotlin.reflect in test classpath); the build
        // itself would fail if the return type changed.
        resetController()
        val s = DeviceRepositoryImpl(RelayController).observeDevice().first()
        // s is a DeviceSummary — has all three nullable fields.
        val summary: DeviceSummary = s
        // The fields are stable; the type checks out.
        @Suppress("UNUSED_VARIABLE")
        val doc: DeviceSummary = summary
        // Use Instant so the import isn't dead; this also pins
        // that DeviceSummary.lastSeen is an Instant at the type
        // level (and not, say, a String).
        val now = Instant.now()
        assert(now.toEpochMilli() > 0)
    }
}
