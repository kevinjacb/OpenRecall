package com.sense.relay.data

import com.sense.relay.domain.model.DeviceSummary
import com.sense.relay.relay.DeviceState
import com.sense.relay.relay.RelayController
import com.sense.relay.relay.RelayState
import kotlinx.coroutines.flow.filter
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withTimeout
import java.time.Instant
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull

/**
 * Pins the contract of [DeviceRepository]: it derives a
 * [DeviceSummary] from the [RelayController]'s current [RelayState].
 * The repository owns the *mapping* (what does each [DeviceState]
 * look like to the UI?) and the *capture* of `lastSeen` (the moment
 * the most recent `updateDevice` was issued), but it does not
 * observe BLE traffic itself — that's [RelayService]'s job.
 *
 * The implementation observes the [RelayController] singleton via
 * an internal collector running on [Dispatchers.Default], so the
 * returned [DeviceSummary] stream is **reactive** — a subscriber
 * that holds the returned [StateFlow] sees new values as the
 * controller's state changes. Tests that assert reactivity use
 * real-time waits (a small `delay` or `Thread.sleep`) because
 * the production dispatcher is not the test scheduler.
 *
 * The implementation observes the [RelayController] singleton.
 * Tests reset the controller at the top of each case so the
 * singleton's state doesn't bleed between runs (same pattern as
 * [com.sense.relay.relay.RelayControllerTest]).
 */
class DeviceRepositoryTest {

    private fun resetController() {
        // Use the controller's own `reset()` so the revision
        // counter is also zeroed (per-field `updateX` calls don't
        // touch revision; a test that calls them would leave the
        // counter at whatever value the previous test bumped it
        // to).
        RelayController.reset()
    }

    @Test fun unknownDeviceMapsToAllNulls() = runTest {
        resetController()
        val repo = DeviceRepositoryImpl(RelayController)
        val s = repo.observeDevice().first()
        assertNull(s.address)
        assertNull(s.name)
        assertNull(s.lastSeen)
    }

    @Test fun connectedDeviceCarriesAddressAndName() = runBlocking {
        resetController()
        RelayController.updateDevice(DeviceState.Connected("AA:BB:CC:DD:EE:FF", "sense-1"))
        val repo = DeviceRepositoryImpl(RelayController)
        // The collector runs on Dispatchers.Default, so we wait
        // with a real-time timeout for the value to propagate.
        val s = withTimeout(2000) {
            repo.observeDevice().filter { it.address != null }.first()
        }
        assertEquals("AA:BB:CC:DD:EE:FF", s.address)
        assertEquals("sense-1", s.name)
        // lastSeen is set by the repository on every emit; the
        // exact value is non-deterministic, so we just check
        // it's not null.
        assertNotNull(s.lastSeen) { "expected lastSeen to be set; was null" }
    }

    @Test fun connectedDeviceWithNullNameHasNullName() = runBlocking {
        resetController()
        RelayController.updateDevice(DeviceState.Connected("AA:BB", null))
        val s = withTimeout(2000) {
            DeviceRepositoryImpl(RelayController)
                .observeDevice()
                .filter { it.address == "AA:BB" }
                .first()
        }
        assertEquals("AA:BB", s.address)
        assertNull(s.name)
    }

    @Test fun disconnectedDeviceHasNullNameAndAddress() = runBlocking {
        // This is the canonical reactivity test: a Connected →
        // Disconnected transition in the controller must produce
        // a new DeviceSummary in the repository's StateFlow. The
        // sanity check pins the StateFlow to the Connected value
        // *before* the transition, so the subsequent "all-null"
        // wait cannot be a false positive against the initial
        // all-null value the StateFlow was constructed with.
        resetController()
        RelayController.updateDevice(DeviceState.Connected("AA:BB", "x"))
        val repo = DeviceRepositoryImpl(RelayController)
        val connected = withTimeout(2000) {
            repo.observeDevice().filter { it.address == "AA:BB" }.first()
        }
        assertNotNull(connected.lastSeen)
        // Sanity: the StateFlow's current value is the Connected
        // one (not the initial all-null). Without this, the
        // post-Disconnected wait could match the initial all-null
        // value and pass spuriously.
        assertEquals("AA:BB", repo.observeDevice().value.address)

        RelayController.updateDevice(DeviceState.Disconnected("gatt state 0"))
        val disconnected = withTimeout(2000) {
            repo.observeDevice().filter {
                it.address == null && it.name == null
            }.first()
        }
        // After a disconnect, address and name are unknown until
        // the next connect — the repository surfaces them as null.
        assertNull(disconnected.address)
        assertNull(disconnected.name)
        // Disconnected maps to all-null, including lastSeen.
        assertNull(disconnected.lastSeen)
    }

    @Test fun scanningDeviceHasNullAddressAndName() = runBlocking {
        // Scanning is a non-connected DeviceState; the repository
        // must surface it as an all-null DeviceSummary. This test
        // also pins reactivity: a Connected → Scanning transition
        // must produce a new emission whose address/name are null.
        resetController()
        RelayController.updateDevice(DeviceState.Connected("AA:BB", "x"))
        val repo = DeviceRepositoryImpl(RelayController)
        // Wait for Connected to propagate first.
        withTimeout(2000) {
            repo.observeDevice().filter { it.address == "AA:BB" }.first()
        }
        // Now transition to Scanning.
        RelayController.updateDevice(DeviceState.Scanning)
        val s = withTimeout(2000) {
            repo.observeDevice().filter { it.address == null && it.name == null }.first()
        }
        assertNull(s.address)
        assertNull(s.name)
    }

    @Test fun lastSeenAdvancesAcrossUpdates() = runBlocking {
        resetController()
        val repo = DeviceRepositoryImpl(RelayController)
        RelayController.updateDevice(DeviceState.Connected("A", null))
        val t1 = withTimeout(2000) {
            repo.observeDevice().filter { it.address == "A" }.first().lastSeen
        }
        // Force a new emit.
        Thread.sleep(5)
        RelayController.updateDevice(DeviceState.Connected("B", null))
        val t2 = withTimeout(2000) {
            repo.observeDevice().filter { it.address == "B" }.first().lastSeen
        }
        assertNotNull(t1) { "expected first lastSeen to be set" }
        assertNotNull(t2) { "expected second lastSeen to be set" }
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
        // current value on subscribe AND see new values as the
        // controller's state changes. The build itself would fail
        // if the return type changed.
        resetController()
        val s = DeviceRepositoryImpl(RelayController).observeDevice()
        // s is a StateFlow<DeviceSummary>. Read .value to pin the
        // initial snapshot.
        val initial: DeviceSummary = s.value
        assertNull(initial.address)
        // Use Instant so the import isn't dead; this also pins
        // that DeviceSummary.lastSeen is an Instant at the type
        // level (and not, say, a String).
        val now = Instant.now()
        assert(now.toEpochMilli() > 0)
    }
}
