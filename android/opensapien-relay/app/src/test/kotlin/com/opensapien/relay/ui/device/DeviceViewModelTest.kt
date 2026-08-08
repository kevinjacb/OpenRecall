package com.opensapien.relay.ui.device

import com.opensapien.relay.core.model.ApiError
import com.opensapien.relay.core.result.Outcome
import com.opensapien.relay.data.DeviceRepository
import com.opensapien.relay.data.StatusRepository
import com.opensapien.relay.domain.model.DeviceSummary
import com.opensapien.relay.domain.model.ServerStatus
import com.opensapien.relay.relay.DeviceState
import com.opensapien.relay.relay.RelayConnectionState
import com.opensapien.relay.relay.RelayController
import com.opensapien.relay.relay.RelayState
import com.opensapien.relay.relay.RelayStarter
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertNull

/**
 * Pins [DeviceViewModel]: it independently carries the relay state, the
 * polled server status, and the device summary (for `lastSeen`).
 *
 * The non-seed tests use a [MutableStateFlow] for the status (it replays its
 * value, so `combine` always sees it) and read `vm.state.value` after the
 * scheduler advances — the Phase-4 `HomeViewModelTest` pattern. The seed
 * test uses a COLD flow that never emits, so `combine` never fires and the
 * state stays at the `stateIn` seed — the production window before the first
 * poll (the production `PollingStatusRepository.observeStatus()` is a
 * `filterNotNull()` that emits nothing until the first poll). A collecting
 * subscriber is held in `backgroundScope` so `stateIn(WhileSubscribed)`
 * actually collects the upstream. The controller is reset between tests.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class DeviceViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    @BeforeTest
    fun setUp() {
        Dispatchers.setMain(dispatcher)
        RelayController.reset()
    }

    @AfterTest
    fun tearDown() = Dispatchers.resetMain()

    /** Status source backed by a StateFlow (replays its current value). */
    private class StateStatusRepository(initial: Outcome<ServerStatus>) : StatusRepository {
        val flow = MutableStateFlow(initial)
        var refreshCount = 0
        override fun observeStatus(): Flow<Outcome<ServerStatus>> = flow
        override suspend fun refresh() { refreshCount++ }
    }

    /** Status source that NEVER emits (a cold flow that suspends forever) —
     *  mirrors the production pre-first-poll silence so the seed is visible. */
    private object SilentStatusRepository : StatusRepository {
        override fun observeStatus(): Flow<Outcome<ServerStatus>> = flow { /* never emits */ }
    }

    private class FakeRelayStarter : RelayStarter {
        var startCount = 0
        override fun start() { startCount++ }
    }

    private class FakeDeviceRepository(initial: DeviceSummary) : DeviceRepository {
        val flow = MutableStateFlow(initial)
        override fun observeDevice(): StateFlow<DeviceSummary> = flow.asStateFlow()
    }

    private fun status(active: Int) = ServerStatus(
        reachable = true, authenticated = true, version = "0.1.0",
        uptimeSeconds = 1, activeSessions = active, totalSessions = 2, recentEvents24h = 3,
    )

    /** Subscribe so `stateIn(WhileSubscribed)` collects the upstream. */
    private fun subscribe(scope: CoroutineScope, vm: DeviceViewModel) {
        scope.launch { vm.state.toList(mutableListOf()) }
    }

    @Test fun seedHoldsBeforeTheFirstStatusEmission() = runTest(dispatcher) {
        // The status flow never emits, so `combine` never fires and the state
        // stays at the `stateIn` seed — the production window before the
        // first poll. This pins the seed handling (a StateFlow-backed fake
        // would emit immediately and overwrite the seed, hiding it).
        val vm = DeviceViewModel(
            RelayController,
            SilentStatusRepository,
            FakeDeviceRepository(DeviceSummary(null, null, null)),
            FakeRelayStarter(),
        )
        subscribe(backgroundScope, vm)
        testScheduler.advanceUntilIdle()
        val s = vm.state.value
        assertEquals(RelayState.Initial, s.relay)
        assertEquals(null, s.device.address, "seed device is all-null")
        assertNull(s.device.lastSeen)
        val failure = assertIs<Outcome.Failure>(s.server)
        val unreachable = assertIs<ApiError.Unreachable>(failure.error)
        assertEquals("loading", unreachable.reason, "seed server is the loading marker")
    }

    @Test fun statusEmissionUpdatesServerWhileRelayUnchanged() = runTest(dispatcher) {
        val status = StateStatusRepository(Outcome.Failure(ApiError.Unreachable("loading")))
        val vm = DeviceViewModel(
            RelayController,
            status,
            FakeDeviceRepository(DeviceSummary(null, null, null)),
            FakeRelayStarter(),
        )
        subscribe(backgroundScope, vm)
        testScheduler.advanceUntilIdle()

        status.flow.value = Outcome.Success(status(5))
        testScheduler.advanceUntilIdle()
        val s = vm.state.value
        assertEquals(RelayState.Initial, s.relay, "relay unchanged")
        val success = assertIs<Outcome.Success<ServerStatus>>(s.server)
        assertEquals(5, success.value.activeSessions)
    }

    @Test fun relayChangeUpdatesRelayWhileServerUnchanged() = runTest(dispatcher) {
        val status = StateStatusRepository(Outcome.Success(status(1)))
        val device = FakeDeviceRepository(DeviceSummary(null, null, null))
        val vm = DeviceViewModel(RelayController, status, device, FakeRelayStarter())
        subscribe(backgroundScope, vm)
        testScheduler.advanceUntilIdle()

        RelayController.updateDevice(DeviceState.Connected("AA:BB", "sense-1"))
        RelayController.updateConnection(RelayConnectionState.Live("s1", 0))
        testScheduler.advanceUntilIdle()
        val s = vm.state.value
        val dev = assertIs<DeviceState.Connected>(s.relay.device)
        assertEquals("AA:BB", dev.address)
        val conn = assertIs<RelayConnectionState.Live>(s.relay.connection)
        assertEquals("s1", conn.sessionId)
        // Server unchanged.
        assertIs<Outcome.Success<ServerStatus>>(s.server)
    }

    @Test fun deviceLastSeenFlowsThroughFromDeviceRepository() = runTest(dispatcher) {
        val status = StateStatusRepository(Outcome.Success(status(1)))
        val device = FakeDeviceRepository(DeviceSummary(null, null, null))
        val vm = DeviceViewModel(RelayController, status, device, FakeRelayStarter())
        subscribe(backgroundScope, vm)
        testScheduler.advanceUntilIdle()

        val seen = java.time.Instant.ofEpochSecond(1700_000_000L)
        device.flow.value = DeviceSummary(address = "AA:BB", name = "sense-1", lastSeen = seen)
        testScheduler.advanceUntilIdle()
        val s = vm.state.value
        assertEquals("AA:BB", s.device.address)
        assertEquals(seen, s.device.lastSeen)
    }

    @Test fun bothPresentAreCarriedTogether() = runTest(dispatcher) {
        val status = StateStatusRepository(Outcome.Success(status(7)))
        val vm = DeviceViewModel(
            RelayController,
            status,
            FakeDeviceRepository(DeviceSummary(null, null, null)),
            FakeRelayStarter(),
        )
        subscribe(backgroundScope, vm)
        RelayController.updateConnection(RelayConnectionState.Live("sx", 100))
        testScheduler.advanceUntilIdle()
        val s = vm.state.value
        assertEquals("sx", (s.relay.connection as RelayConnectionState.Live).sessionId)
        assertEquals(7, (s.server as Outcome.Success).value.activeSessions)
    }

    @Test fun onRefreshForcesStatusPollAndTogglesIsRefreshing() = runTest(dispatcher) {
        val status = StateStatusRepository(Outcome.Success(status(1)))
        val vm = DeviceViewModel(
            RelayController,
            status,
            FakeDeviceRepository(DeviceSummary(null, null, null)),
            FakeRelayStarter(),
        )
        subscribe(backgroundScope, vm)
        testScheduler.advanceUntilIdle()
        assertFalse(vm.isRefreshing.value, "not refreshing before the gesture")

        vm.onRefresh()
        testScheduler.advanceUntilIdle()

        assertEquals(1, status.refreshCount, "status.refresh() invoked once")
        assertFalse(vm.isRefreshing.value, "isRefreshing cleared after the refresh")
    }

    @Test fun onRetryConnectionCallsRelayStarter() = runTest(dispatcher) {
        val starter = FakeRelayStarter()
        val vm = DeviceViewModel(
            RelayController,
            StateStatusRepository(Outcome.Success(status(1))),
            FakeDeviceRepository(DeviceSummary(null, null, null)),
            starter,
        )
        vm.onRetryConnection()
        assertEquals(1, starter.startCount, "relayStarter.start() invoked once")
    }
}